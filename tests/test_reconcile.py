import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from reconcile import audit, build, cents, load_csv, bundle_options, MAX_GROUP


def invoice(i, amount, customer='A', currency='USD'):
    return {'invoice_id': i, 'customer_id': customer, 'cents': amount, 'currency': currency}


def payment(i, amount, payer='Alpha Demo', currency='USD'):
    return {'payment_id': i, 'payer': payer, 'cents': amount, 'currency': currency}


class AuditTest(unittest.TestCase):
    aliases = [{'payer': 'Alpha Demo', 'customer_id': 'A'}]

    def test_two_exact_bundles_are_not_a_unique_match(self):
        inv = [invoice('I1', 60000), invoice('I2', 90000), invoice('I3', 150000)]
        row = audit(inv, [payment('P1', 150000)], self.aliases)['payments'][0]
        self.assertEqual(row['status'], 'REVIEW')
        self.assertEqual(row['options'], [['I3'], ['I1', 'I2']])

    def test_unique_bundle_and_cent_precision(self):
        inv = [invoice('I1', cents('0.10')), invoice('I2', cents('0.20'))]
        row = audit(inv, [payment('P1', cents('0.30'), 'ALPHA-DEMO')], self.aliases)['payments'][0]
        self.assertEqual(row['status'], 'SUGGESTED')
        self.assertEqual(row['options'], [['I1', 'I2']])

    def test_competing_payments_hold_both_and_are_order_independent(self):
        inv = [invoice('I1', 10000)]
        pay = [payment('P1', 10000), payment('P2', 10000)]
        first = audit(inv, pay, self.aliases)
        self.assertTrue(all(r['status'] == 'REVIEW' for r in first['payments']))
        self.assertEqual(first, audit(inv[::-1], pay[::-1], self.aliases))

    def test_unresolved_payment_quarantines_group_not_unrelated_customers(self):
        inv = [invoice('I1', 10000), invoice('I2', 20000), invoice('B1', 5000, 'B')]
        pay = [payment('P1', 10000), payment('P2', 500), payment('P3', 5000, 'Beta Demo')]
        aliases = self.aliases + [{'payer': 'Beta Demo', 'customer_id': 'B'}]
        rows = audit(inv, pay, aliases)['payments']
        self.assertEqual([r['status'] for r in rows], ['REVIEW', 'REVIEW', 'SUGGESTED'])

    def test_shared_alias_does_not_choose_first_customer(self):
        inv = [invoice('I1', 10000), invoice('B1', 10000, 'B')]
        aliases = self.aliases + [{'payer': 'alpha demo', 'customer_id': 'B'}]
        r = audit(inv, [payment('P1', 10000)], aliases)['payments'][0]
        self.assertEqual(r['candidate_customers'], ['A', 'B'])
        self.assertEqual(r['status'], 'REVIEW')

    def test_no_substring_guess_and_unknown_quarantines_currency(self):
        inv = [invoice('I1', 10000), invoice('I2', 5000)]
        rows = audit(inv, [payment('P1', 10000), payment('P2', 5000, 'Alpha')], self.aliases)['payments']
        self.assertTrue(all(r['status'] == 'REVIEW' for r in rows))

    def test_currency_is_not_mixed(self):
        inv = [invoice('I1', 10000, currency='EUR')]
        r = audit(inv, [payment('P1', 10000)], self.aliases)['payments'][0]
        self.assertEqual(r['status'], 'REVIEW')
        self.assertEqual(r['options'], [])

    def test_search_limit_is_review_not_false_unique(self):
        inv = [invoice('I' + str(i), i + 1) for i in range(MAX_GROUP + 1)]
        self.assertIsNone(bundle_options(inv, 1))
        r = audit(inv, [payment('P1', 1)], self.aliases)['payments'][0]
        self.assertEqual(r['status'], 'REVIEW')
        self.assertIn('More than', r['reason'])

    def test_invalid_money_and_duplicate_ids(self):
        for value in ['NaN', 'inf', '-1', '1.001', '1e2', '0', '1,000', '=SUM(1)']:
            with self.subTest(value=value), self.assertRaises(ValueError): cents(value)
        data = b'invoice_id,customer_id,amount,currency\nI1,A,1.00,USD\ni1,A,2.00,USD\n'
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            load_csv(data, ['invoice_id','customer_id','amount','currency'], 'invoice_id')

    def test_real_demo_exports_only_unambiguous_disjoint_allocations(self):
        fixtures = Path(__file__).resolve().parents[1]/'examples'
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'report'
            plan = build(fixtures/'invoices.csv', fixtures/'payments.csv', fixtures/'aliases.csv', out)
            self.assertEqual(plan['totals_cents']['USD'], {'SUGGESTED':170000, 'REVIEW':410000})
            rows = list(csv.DictReader(io.StringIO((out/'suggestions.csv').read_text())))
            self.assertEqual({r['payment_id'] for r in rows}, {'PAY-02','PAY-05'})
            self.assertEqual(len({r['invoice_id'] for r in rows}), len(rows))
            self.assertTrue((out/'READY.json').is_file())
            with self.assertRaises(FileExistsError):
                build(fixtures/'invoices.csv', fixtures/'payments.csv', fixtures/'aliases.csv', out)
            self.assertEqual(len(list(out.glob('*.png'))), 2)

    def test_renderer_failure_leaves_no_partial_result_and_retry_succeeds(self):
        fixtures = Path(__file__).resolve().parents[1]/'examples'
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)/'report'
            with patch('report.render', side_effect=RuntimeError('test rendering failure')):
                with self.assertRaises(RuntimeError):
                    build(fixtures/'invoices.csv', fixtures/'payments.csv', fixtures/'aliases.csv', out)
            self.assertFalse(out.exists())
            build(fixtures/'invoices.csv', fixtures/'payments.csv', fixtures/'aliases.csv', out)
            self.assertTrue((out/'READY.json').exists())


if __name__ == '__main__': unittest.main()
