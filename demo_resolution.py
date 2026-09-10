"""An invented reviewer choice resolves one hold, while stale choices are refused."""
import argparse
import hashlib
import json
from pathlib import Path
from reconcile import build, json_bytes


def run(output):
    fixtures = Path(__file__).parent/'examples'
    output.mkdir(mode=0o700)
    inputs = [fixtures/name for name in ('invoices.csv', 'payments.csv', 'aliases.csv')]
    before = output/'before'
    build(*inputs, before)
    original_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in before.iterdir()}
    decisions = json.loads((before/'decisions-template.json').read_text())
    decisions['selections'] = [{'payment_id': 'PAY-01', 'invoice_ids': ['A-01', 'A-02'],
                                'evidence': 'Fictional remittance lists A-01 and A-02.'}]
    decision_path = output/'reviewer-decisions.json'
    decision_path.write_bytes(json_bytes(decisions))
    after = build(*inputs, output/'after', decision_path)
    assert [r['status'] for r in after['payments']] == ['SELECTED', 'SUGGESTED', 'REVIEW', 'REVIEW', 'SUGGESTED', 'REVIEW']
    assert after['totals_cents'] == {'USD': {'SUGGESTED': 170000, 'REVIEW': 260000, 'SELECTED': 150000}}
    changed = output/'changed-invoices.csv'
    changed.write_bytes(inputs[0].read_bytes() + b'\n')
    try:
        build(changed, *inputs[1:], output/'stale-result', decision_path)
    except ValueError as exc:
        assert str(exc).startswith('Stale decisions:')
        rejection = str(exc)
    else:
        raise AssertionError('Changed inputs accepted a stale decision')
    assert not (output/'stale-result').exists()
    assert original_hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in before.iterdir()}
    assert (output/'after'/'original-plan.json').read_bytes() == (before/'plan.json').read_bytes()
    proof = {'verified_demo': True, 'totals_cents': after['totals_cents'],
             'original_report_unchanged': True, 'stale_choice_rejected': rejection}
    (output/'demo-proof.json').write_bytes(json_bytes(proof))
    return proof


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.out)))
