import copy
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from reconcile import apply_decisions, audit, build, decision_template, json_bytes, load_csv
from demo_resolution import run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT/'examples'
INPUTS = [FIXTURES/name for name in ('invoices.csv', 'payments.csv', 'aliases.csv')]


class DecisionTest(unittest.TestCase):
    def setUp(self):
        data = [p.read_bytes() for p in INPUTS]
        invoices = load_csv(data[0], ['invoice_id', 'customer_id', 'amount', 'currency'], 'invoice_id')
        payments = load_csv(data[1], ['payment_id', 'payer', 'amount', 'currency'], 'payment_id')
        aliases = load_csv(data[2], ['payer', 'customer_id'])
        self.plan = audit(invoices, payments, aliases)
        self.plan['input_sha256'] = dict(zip(['invoices', 'payments', 'aliases'],
                                           [hashlib.sha256(d).hexdigest() for d in data]))
        self.decisions = decision_template(self.plan)
        self.decisions['selections'] = [self.choice('PAY-01', ['A-01', 'A-02'])]

    def choice(self, payment, invoices):
        return {'payment_id': payment, 'invoice_ids': invoices, 'evidence': 'Invented reviewed remittance.'}

    def apply(self):
        return apply_decisions(self.plan, json_bytes(self.decisions))

    def test_choice_preserves_original_and_does_not_unlock_other_holds(self):
        original = copy.deepcopy(self.plan)
        result = self.apply()
        self.assertEqual(self.plan, original)
        self.assertEqual([r['status'] for r in result['payments']],
                         ['SELECTED', 'SUGGESTED', 'REVIEW', 'REVIEW', 'SUGGESTED', 'REVIEW'])
        self.assertEqual(result['totals_cents']['USD'], {'SUGGESTED': 170000, 'REVIEW': 260000, 'SELECTED': 150000})
        self.assertEqual(result['payments'][0]['options'], original['payments'][0]['options'])
        self.assertEqual(result['decision_provenance']['original_plan_sha256'], hashlib.sha256(json_bytes(original)).hexdigest())

    def test_each_input_and_original_plan_hash_is_bound(self):
        for key in ['invoices', 'payments', 'aliases']:
            with self.subTest(key=key):
                decision = copy.deepcopy(self.decisions)
                decision['input_sha256'][key] = '0' * 64
                with self.assertRaisesRegex(ValueError, 'Stale decisions'):
                    apply_decisions(self.plan, json_bytes(decision))
        self.plan['payments'][0]['reason'] = 'Changed review logic'
        with self.assertRaisesRegex(ValueError, 'Stale decisions'): self.apply()

    def test_competing_choice_is_explicit_and_other_claimant_stays_held(self):
        self.decisions['selections'] = [self.choice('PAY-03', ['C-01'])]
        result = self.apply()
        self.assertEqual([r['status'] for r in result['payments'][2:4]], ['SELECTED', 'REVIEW'])
        self.decisions['selections'].append(self.choice('PAY-04', ['C-01']))
        with self.assertRaisesRegex(ValueError, 'Invoice reuse'): self.apply()

    def test_wrong_amount_customer_currency_and_unknown_invoice_are_rejected(self):
        for ids, message in [(['A-01'], 'equal'), (['B-01', 'B-02'], 'customer'),
                             (['MISSING'], 'known invoice'), (['A-01', 'A-01'], 'Duplicate invoice')]:
            with self.subTest(ids=ids):
                self.decisions['selections'] = [self.choice('PAY-01', ids)]
                with self.assertRaisesRegex(ValueError, message): self.apply()
        self.plan['invoices'][2]['currency'] = 'EUR'
        self.decisions = decision_template(self.plan)
        self.decisions['selections'] = [self.choice('PAY-01', ['A-03'])]
        with self.assertRaisesRegex(ValueError, 'currency'): self.apply()

    def test_alias_ambiguity_cannot_be_overridden_by_a_selection(self):
        self.plan['payments'][0]['customer_id'] = None
        self.decisions = decision_template(self.plan)
        self.decisions['selections'] = [self.choice('PAY-01', ['A-03'])]
        with self.assertRaisesRegex(ValueError, 'payer alias'): self.apply()

    def test_choice_does_not_have_to_be_one_of_two_displayed_witnesses(self):
        # Explicit review may identify a third valid bundle the bounded display omits.
        self.plan['invoices'].append({'invoice_id': 'A-04', 'customer_id': 'ACORN', 'currency': 'USD', 'cents': 150000})
        self.decisions = decision_template(self.plan)
        self.decisions['selections'] = [self.choice('PAY-01', ['A-04'])]
        self.assertEqual(self.apply()['payments'][0]['selected_invoice_ids'], ['A-04'])

    def test_duplicate_payment_and_suggested_payment_cannot_be_selected(self):
        self.decisions['selections'] *= 2
        with self.assertRaisesRegex(ValueError, 'Duplicate payment'): self.apply()
        self.decisions['selections'] = [self.choice('PAY-02', ['B-01', 'B-02'])]
        with self.assertRaisesRegex(ValueError, 'Only held'): self.apply()

    def test_choices_cannot_reuse_an_existing_suggestion(self):
        # Defense in depth if future audit logic produces a held and suggested overlap.
        self.plan['payments'][1]['options'] = [['A-03']]
        self.decisions = decision_template(self.plan)
        self.decisions['selections'] = [self.choice('PAY-01', ['A-03'])]
        with self.assertRaisesRegex(ValueError, 'Invoice reuse'): self.apply()

    def test_malformed_decisions_fail_with_actionable_value_errors(self):
        for value in [None, [], {}, {'schema_version': 1}, {**self.decisions, 'selections': []},
                      {**self.decisions, 'schema_version': True},
                      {**self.decisions, 'selections': [self.choice([], ['A-03'])]},
                      {**self.decisions, 'selections': [self.choice('PAY-01', [[]])]},
                      {**self.decisions, 'selections': [{**self.choice('PAY-01', ['A-03']), 'evidence': ''}]}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                apply_decisions(self.plan, json_bytes(value))
        with self.assertRaisesRegex(ValueError, 'Duplicate decision field'):
            apply_decisions(self.plan, b'{"schema_version":1,"schema_version":2}')

    def test_full_demo_preserves_original_and_exports_separate_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)/'demo'
            proof = run(output)
            self.assertTrue(proof['original_report_unchanged'])
            self.assertTrue(proof['stale_choice_rejected'])
            after = output/'after'
            with (after/'selected.csv').open() as f: rows = list(csv.DictReader(f))
            self.assertEqual([r['invoice_id'] for r in rows], ['A-01', 'A-02'])
            expected_hash = hashlib.sha256((output/'reviewer-decisions.json').read_bytes()).hexdigest()
            self.assertTrue(all(r['decisions_sha256'] == expected_hash for r in rows))
            with (after/'suggestions.csv').open() as f: suggested = list(csv.DictReader(f))
            all_rows = rows + suggested
            self.assertEqual(len({r['invoice_id'] for r in all_rows}), len(all_rows))
            self.assertFalse((after/'ambiguity.png').exists())
            self.assertTrue((after/'resolution.png').exists())
            ready = json.loads((after/'READY.json').read_text())
            for name, digest in ready['files'].items():
                self.assertEqual(hashlib.sha256((after/name).read_bytes()).hexdigest(), digest)
            result = subprocess.run([sys.executable, str(ROOT/'reconcile.py'), '--invoices', str(output/'changed-invoices.csv'),
                                     '--payments', str(INPUTS[1]), '--aliases', str(INPUTS[2]),
                                     '--decisions', str(output/'reviewer-decisions.json'), '--out', str(output/'cli-stale')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('Stale decisions', result.stderr)
            self.assertFalse((output/'cli-stale').exists())


if __name__ == '__main__': unittest.main()
