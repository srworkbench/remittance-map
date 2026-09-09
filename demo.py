"""Run the invented example through the real audit and report generator."""
import argparse
import json
from pathlib import Path
from reconcile import build

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    fixtures = Path(__file__).parent/'examples'
    plan = build(fixtures/'invoices.csv', fixtures/'payments.csv', fixtures/'aliases.csv', args.out)
    assert [r['status'] for r in plan['payments']] == ['REVIEW', 'SUGGESTED', 'REVIEW', 'REVIEW', 'SUGGESTED', 'REVIEW']
    assert plan['totals_cents'] == {'USD': {'SUGGESTED': 170000, 'REVIEW': 410000}}
    print(json.dumps({'verified_demo': True, 'totals_cents': plan['totals_cents'], 'payments': len(plan['payments'])}))
