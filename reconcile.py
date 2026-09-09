"""Exact-cent invoice bundle review. Suggestions only; never posts transactions."""
from __future__ import annotations

import argparse
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


def build(invoice_path, payment_path, alias_path, output):
    from report import render
    paths = [Path(p) for p in (invoice_path, payment_path, alias_path)]
    data = [p.read_bytes() for p in paths]
    invoices = load_csv(data[0], ['invoice_id', 'customer_id', 'amount', 'currency'], 'invoice_id')
    payments = load_csv(data[1], ['payment_id', 'payer', 'amount', 'currency'], 'payment_id')
    aliases = load_csv(data[2], ['payer', 'customer_id'])
    plan = audit(invoices, payments, aliases)
    plan['input_sha256'] = dict(zip(['invoices', 'payments', 'aliases'], [hashlib.sha256(d).hexdigest() for d in data]))
    output = Path(output)
    # Atomic exclusive reservation. Never overwrite another result, even an empty directory.
    output.mkdir(mode=0o700)
    try:
        (output/'plan.json').write_text(json.dumps(plan, indent=2) + '\n', encoding='utf-8')
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
    args = parser.parse_args()
    try:
        plan = build(args.invoices, args.payments, args.aliases, args.out)
    except (ValueError, OSError, UnicodeError, csv.Error) as exc:
        parser.exit(2, f'Review failed: {exc}\n')
    print(json.dumps({'payments': len(plan['payments']), 'totals_cents': plan['totals_cents'], 'mode': plan['mode']}))


if __name__ == '__main__':
    main()
