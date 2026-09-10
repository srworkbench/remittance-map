"""Exact-cent invoice bundle review. Suggestions only; never posts transactions."""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import io
import itertools
import json
from pathlib import Path
import re
import shutil
import unicodedata

MAX_GROUP = 16
MAX_ROWS = 1000


def normalized(text):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', text).casefold()))


def cents(value):
    if not re.fullmatch(r'\d{1,10}(?:\.\d{1,2})?', value):
        raise ValueError('Amounts must be positive decimals with at most two decimal places')
    whole, _, fraction = value.partition('.')
    result = int(whole) * 100 + int(fraction.ljust(2, '0') or '0')
    if result <= 0:
        raise ValueError('Amounts must be greater than zero')
    return result


def money(amount):
    return f'{amount // 100:,}.{amount % 100:02d}'


def load_csv(data, fields, key=None):
    reader = csv.DictReader(io.StringIO(data.decode('utf-8-sig'), newline=''))
    if reader.fieldnames != fields:
        raise ValueError('CSV columns must be exactly: ' + ','.join(fields))
    rows = []
    seen = set()
    for row in reader:
        if len(rows) >= MAX_ROWS:
            raise ValueError(f'CSV limit is {MAX_ROWS} rows')
        if None in row or any(v is None for v in row.values()):
            raise ValueError('CSV row has missing or extra fields')
        row = {k: v.strip() for k, v in row.items()}
        if any(not v or len(v) > 160 or any(ord(c) < 32 for c in v) for v in row.values()):
            raise ValueError('CSV values must be nonempty, single-line and at most 160 characters')
        if key:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', row[key]):
                raise ValueError('IDs must be short letters, digits, underscores or hyphens')
            if row[key].casefold() in seen:
                raise ValueError(f'Duplicate {key}')
            seen.add(row[key].casefold())
        if 'amount' in row:
            row['cents'] = cents(row.pop('amount'))
            if not re.fullmatch(r'[A-Z]{3}', row['currency']):
                raise ValueError('Currency must be a three-letter uppercase code')
        rows.append(row)
    if not rows:
        raise ValueError('CSV must contain at least one row')
    return rows


def bundle_options(invoices, target):
    """Return at most two distinct witnesses; two suffice to prove ambiguity."""
    if len(invoices) > MAX_GROUP:
        return None
    options = []
    for size in range(1, len(invoices) + 1):
        for group in itertools.combinations(invoices, size):
            if sum(i['cents'] for i in group) == target:
                options.append([i['invoice_id'] for i in group])
                if len(options) == 2:
                    return options
    return options


def audit(invoices, payments, aliases):
    if len({r['currency'] for r in invoices + payments}) > 8:
        raise ValueError('Review at most eight currencies per run')
    customers = {i['customer_id'] for i in invoices}
    alias_map = {}
    for row in aliases:
        if row['customer_id'] not in customers:
            raise ValueError('Alias refers to a customer missing from the invoice input')
        key = normalized(row['payer'])
        if not key:
            raise ValueError('Alias must contain letters or digits')
        alias_map.setdefault(key, set()).add(row['customer_id'])
    groups = {}
    for invoice in sorted(invoices, key=lambda r: r['invoice_id']):
        groups.setdefault((invoice['customer_id'], invoice['currency']), []).append(invoice)
    results = []
    unsafe_groups = set()
    for payment in sorted(payments, key=lambda r: r['payment_id']):
        r = {**payment, 'status': 'REVIEW', 'options': [], 'reason': '', 'customer_id': None}
        found = sorted(alias_map.get(normalized(payment['payer']), set()))
        r['candidate_customers'] = found
        if len(found) != 1:
            r['reason'] = 'Payer alias is shared by multiple customers' if found else 'Payer has no approved alias'
            # Unknown payer could belong anywhere in this currency; conservatively hold it all.
            unsafe_groups.update((c, payment['currency']) for c in (found or customers))
        else:
            r['customer_id'] = found[0]
            group = (found[0], payment['currency'])
            options = bundle_options(groups.get(group, []), payment['cents'])
            r['options'] = options or []
            if options is None:
                r['reason'] = f'More than {MAX_GROUP} open invoices; split input scope for review'
                unsafe_groups.add(group)
            elif len(options) > 1:
                r['reason'] = 'Multiple invoice bundles have the same total'
                unsafe_groups.add(group)
            elif not options:
                r['reason'] = 'No exact whole-invoice bundle; check partial payments, fees or missing invoices'
                unsafe_groups.add(group)
            else:
                r['status'] = 'SUGGESTED'
                r['reason'] = 'One exact bundle under the supplied payer mapping'
        results.append(r)
    # An unresolved payment may touch invoices not in its first two witnesses.
    # Quarantine its entire customer/currency group instead of claiming those invoices are free.
    for r in results:
        if r['status'] == 'SUGGESTED' and (r['customer_id'], r['currency']) in unsafe_groups:
            r.update(status='REVIEW', reason='Another unresolved payment may use these invoices')
    use = {}
    for r in results:
        if r['status'] == 'SUGGESTED':
            for invoice_id in r['options'][0]:
                use.setdefault(invoice_id, []).append(r)
    for invoice_id, claimants in use.items():
        if len(claimants) > 1:
            ids = sorted(r['payment_id'] for r in claimants)
            for r in claimants:
                r.update(status='REVIEW', reason='Competing payments would reuse an invoice', competing_payments=ids)
    totals = {}
    for r in results:
        bucket = totals.setdefault(r['currency'], {'SUGGESTED': 0, 'REVIEW': 0})
        bucket[r['status']] += r['cents']
    return {'schema_version': 1, 'mode': 'review-only', 'payments': results, 'invoices': invoices,
            'totals_cents': totals, 'limits': {'max_invoices_per_customer_currency': MAX_GROUP,
            'max_rows_per_input': MAX_ROWS, 'options_shown': 'At most two witnesses; not an exhaustive allocation list'}}


def json_bytes(value):
    return (json.dumps(value, indent=2) + '\n').encode('utf-8')


def decision_template(plan):
    return {'schema_version': 1, 'input_sha256': plan['input_sha256'],
            'plan_sha256': hashlib.sha256(json_bytes(plan)).hexdigest(), 'selections': []}


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate decision field: {key}')
        result[key] = value
    return result


def apply_decisions(plan, data):
    """Validate the whole decision file before producing any changed plan.

    A choice is a recorded human selection, never a newly inferred suggestion.
    Unselected holds remain held, including payments competing with a choice.
    """
    decisions = json.loads(data.decode('utf-8'), object_pairs_hook=unique_keys)
    template = decision_template(plan)
    if not isinstance(decisions, dict) or set(decisions) != set(template):
        raise ValueError('Decisions must use the fields from decisions-template.json')
    if type(decisions['schema_version']) is not int or decisions['schema_version'] != 1:
        raise ValueError('Unsupported decision schema version')
    if decisions['input_sha256'] != template['input_sha256'] or decisions['plan_sha256'] != template['plan_sha256']:
        raise ValueError('Stale decisions: inputs or original review changed; review again and copy a fresh template')
    choices = decisions['selections']
    if not isinstance(choices, list) or not 1 <= len(choices) <= len(plan['payments']):
        raise ValueError('Add at least one selection; no more than one per payment')
    rows = {r['payment_id']: r for r in plan['payments']}
    invoices = {i['invoice_id']: i for i in plan['invoices']}
    used = {i for r in plan['payments'] if r['status'] == 'SUGGESTED' for i in r['options'][0]}
    selected = {}
    for choice in choices:
        if not isinstance(choice, dict) or set(choice) != {'payment_id', 'invoice_ids', 'evidence'}:
            raise ValueError('Each selection needs payment_id, invoice_ids and evidence')
        payment_id = choice['payment_id']
        if not isinstance(payment_id, str) or payment_id not in rows:
            raise ValueError('Selection refers to an unknown payment ID')
        if payment_id in selected:
            raise ValueError('Duplicate payment selection')
        row = rows[payment_id]
        if row['status'] != 'REVIEW':
            raise ValueError('Only held payments accept reviewer selections')
        if row['customer_id'] is None:
            raise ValueError('Resolve the payer alias and rerun before selecting invoices')
        ids = choice['invoice_ids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ROWS or
                any(not isinstance(i, str) or i not in invoices for i in ids)):
            raise ValueError('Select a nonempty list of known invoice IDs')
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate invoice in selection')
        if any(invoices[i]['customer_id'] != row['customer_id'] or
               invoices[i]['currency'] != row['currency'] for i in ids):
            raise ValueError('Selected invoices must belong to the payment customer and currency')
        if sum(invoices[i]['cents'] for i in ids) != row['cents']:
            raise ValueError('Selected whole-invoice amounts must equal the payment exactly')
        if used.intersection(ids):
            raise ValueError('Invoice reuse: selections must be disjoint from suggestions and other selections')
        evidence = choice['evidence']
        if (not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 240 or
                any(ord(c) < 32 for c in evidence)):
            raise ValueError('Evidence must be a nonempty single line of at most 240 characters')
        used.update(ids)
        selected[payment_id] = {**choice, 'invoice_ids': sorted(ids)}
    result = copy.deepcopy(plan)
    result['decision_provenance'] = {
        'decisions_sha256': hashlib.sha256(data).hexdigest(),
        'original_plan_sha256': template['plan_sha256'],
        'policy': 'Explicit reviewer selections only; all other held payments remain held'}
    for row in result['payments']:
        if row['payment_id'] in selected:
            choice = selected[row['payment_id']]
            row.update(status='SELECTED', original_status='REVIEW', original_reason=row['reason'],
                       selected_invoice_ids=choice['invoice_ids'], evidence=choice['evidence'],
                       reason='Reviewer selected invoices; no transaction posted')
    for sums in result['totals_cents'].values():
        sums['SELECTED'] = 0
    for row in result['payments']:
        if row['status'] == 'SELECTED':
            sums = result['totals_cents'][row['currency']]
            sums['REVIEW'] -= row['cents']; sums['SELECTED'] += row['cents']
    return result


def build(invoice_path, payment_path, alias_path, output, decisions_path=None):
    from report import render
    paths = [Path(p) for p in (invoice_path, payment_path, alias_path)]
    data = [p.read_bytes() for p in paths]
    invoices = load_csv(data[0], ['invoice_id', 'customer_id', 'amount', 'currency'], 'invoice_id')
    payments = load_csv(data[1], ['payment_id', 'payer', 'amount', 'currency'], 'payment_id')
    aliases = load_csv(data[2], ['payer', 'customer_id'])
    plan = audit(invoices, payments, aliases)
    plan['input_sha256'] = dict(zip(['invoices', 'payments', 'aliases'], [hashlib.sha256(d).hexdigest() for d in data]))
    original = json_bytes(plan)
    decisions_data = Path(decisions_path).read_bytes() if decisions_path is not None else None
    if decisions_data is not None:
        plan = apply_decisions(plan, decisions_data)
    output = Path(output)
    # Atomic exclusive reservation. Never overwrite another result, even an empty directory.
    output.mkdir(mode=0o700)
    try:
        (output/'plan.json').write_bytes(json_bytes(plan))
        if decisions_data is None:
            (output/'decisions-template.json').write_bytes(json_bytes(decision_template(plan)))
        else:
            (output/'original-plan.json').write_bytes(original)
            (output/'decisions.json').write_bytes(decisions_data)
            with (output/'selected.csv').open('w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(['payment_id', 'invoice_id', 'currency', 'decisions_sha256'])
                for row in plan['payments']:
                    if row['status'] == 'SELECTED':
                        for invoice_id in row['selected_invoice_ids']:
                            writer.writerow([row['payment_id'], invoice_id, row['currency'],
                                             plan['decision_provenance']['decisions_sha256']])
        with (output/'suggestions.csv').open('w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f); writer.writerow(['payment_id', 'invoice_id', 'currency'])
            for row in plan['payments']:
                if row['status'] == 'SUGGESTED':
                    for invoice_id in row['options'][0]:
                        writer.writerow([row['payment_id'], invoice_id, row['currency']])
        render(plan, output)
        # A ready marker is written last; consumers must require it.
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir())}
        (output/'READY.json').write_text(json.dumps({'files': files}, indent=2) + '\n')
    except BaseException:
        shutil.rmtree(output)
        raise
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--invoices', type=Path, required=True)
    parser.add_argument('--payments', type=Path, required=True)
    parser.add_argument('--aliases', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--decisions', type=Path, help='Completed decisions template from the original review')
    args = parser.parse_args()
    try:
        plan = build(args.invoices, args.payments, args.aliases, args.out, args.decisions)
    except (ValueError, OSError, UnicodeError, csv.Error) as exc:
        parser.exit(2, f'Review failed: {exc}\n')
    print(json.dumps({'payments': len(plan['payments']), 'totals_cents': plan['totals_cents'], 'mode': plan['mode']}))


if __name__ == '__main__':
    main()
