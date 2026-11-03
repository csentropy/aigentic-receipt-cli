"""Evidence and adapter regression checks; fixtures contain no real user data."""
import copy
import importlib.util
import json
import sqlite3
import subprocess
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location('receipt', ROOT / 'aigentic.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)
SAMPLE = json.loads((ROOT / 'sample-receipt.json').read_text())

class Receipts(unittest.TestCase):
    def test_browser_cli_parity_and_evidence_failures(self):
        cases = [copy.deepcopy(SAMPLE)]
        for field, value in [('cost', None), ('cost_basis', 'estimated'), ('model', 'mixed'), ('model', 'UNKNOWN')]:
            row = copy.deepcopy(SAMPLE); row['tasks'][0][field] = value; cases.append(row)
        for field, value in [('billing', 'subscription'), ('coverage', 'partial')]:
            row = copy.deepcopy(SAMPLE); row[field] = value; cases.append(row)
        row = copy.deepcopy(SAMPLE); row['tasks'][0]['comparison']['checks'][0]['candidate_passed'] = False; cases.append(row)
        row = copy.deepcopy(SAMPLE); row['tasks'][0]['comparison']['workload_sha256'] = 'f' * 64; cases.append(row)
        row = copy.deepcopy(SAMPLE); row['tasks'][1]['comparison']['reference_task_id'] = row['tasks'][0]['comparison']['reference_task_id']; cases.append(row)
        row = copy.deepcopy(SAMPLE); row['tasks'][0]['comparison']['reference_task_id'] = row['tasks'][1]['id']; cases.append(row)
        script = "const core=require('./receipt-core.cjs');let input='';process.stdin.on('data',s=>input+=s);process.stdin.on('end',()=>console.log(JSON.stringify(JSON.parse(input).map(x=>core.summarize(core.validate(x))))));"
        browser = json.loads(subprocess.check_output(['node', '-e', script], input=json.dumps(cases), text=True, cwd=ROOT))
        for i, (value, js) in enumerate(zip(cases, browser)):
            with self.subTest(i=i):
                py = cli.analyze(cli.validate(value))
                self.assertEqual(py['receipt_checks_passed'], js['allVerified'])
                self.assertEqual(py['receipt_checks_passed'], i == 0)
                self.assertEqual(py['verified_savings'] is None, js['verified'] is None)
                if i == 0:
                    self.assertEqual(py['verified_savings'], Decimal('18.42'))
                    self.assertEqual(js['share']['percent'], 43)

    def test_increases_and_missing_costs(self):
        value = copy.deepcopy(SAMPLE)
        value['tasks'][0]['cost'] = 80
        result = cli.analyze(cli.validate(value))
        self.assertLess(result['verified_savings'], 0)
        value['tasks'][0]['cost'] = None
        result = cli.analyze(cli.validate(value))
        self.assertIsNone(result['verified_savings'])
        self.assertFalse(result['cost_coverage_complete'])
        self.assertIsNone(result['estimated_additional_savings'])

    def test_openclaw_metadata_only_and_incomplete(self):
        fixture = {'sessions': [{'key': 'private-session-key', 'title': 'SECRET TITLE', 'messages': ['SECRET PROMPT'],
            'usage': {'totalCost': 1.25, 'modelUsage': [{'model': 'a', 'count': 3}]}}]}
        result = cli.openclaw_receipt(fixture, 'background')
        self.assertEqual(result['tasks'][0]['cost_basis'], 'estimated')
        self.assertEqual(result['tasks'][0]['calls'], 3)
        self.assertNotIn('SECRET', cli.as_json(result))
        self.assertNotIn('private-session-key', cli.as_json(result))
        fixture['sessions'][0]['usage']['missingCostEntries'] = 1
        self.assertEqual(cli.openclaw_receipt(fixture, 'background')['coverage'], 'partial')
        fixture['sessions'][0]['usage'] = {}
        self.assertIsNone(cli.openclaw_receipt(fixture, 'background')['tasks'][0]['cost'])

    def test_hermes_schema_scope_privacy_and_missing_billing(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'state.db'
            conn = sqlite3.connect(db)
            conn.execute('CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL, ended_at REAL, parent_session_id TEXT, billing_mode TEXT, estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, api_call_count INTEGER, title TEXT, system_prompt TEXT)')
            now = time.time()
            rows = [('actual','a',now,now,None,'api',2,1,'actual',1),
                    ('multi','a',now,now,None,'api',3,2,'actual',2),
                    ('estimate','a',now,now,None,'api',4,None,'estimated',5),
                    ('unknown','a',now,now,None,'api',0,None,'unknown',2),
                    ('running','a',now,None,None,'api',1,None,'estimated',2),
                    ('oauth','a',now,now,None,'oauth',7,None,'estimated',1),
                    ('included','a',now,now,None,'api',0,0,'included',1),
                    ('child','a',now,now,'actual','api',20,None,'estimated',2),
                    ('old','a',now-9*86400,now,None,'api',20,None,'estimated',1)]
            conn.executemany('INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', [(*r,'SECRET TITLE','SECRET PROMPT') for r in rows]);conn.commit();conn.close()
            before = db.read_bytes()
            value = cli.hermes_receipt(db, 'background', 7)
            self.assertEqual(db.read_bytes(), before)
            self.assertEqual(len(value['tasks']), 5)
            by_id = {r['id']: r for r in value['tasks']}
            self.assertEqual(by_id[cli.private_id('hermes','actual')]['cost_basis'], 'reported')
            self.assertEqual(by_id[cli.private_id('hermes','multi')]['cost_basis'], 'incomplete')
            self.assertEqual(by_id[cli.private_id('hermes','estimate')]['cost_basis'], 'estimated')
            self.assertIsNone(by_id[cli.private_id('hermes','unknown')]['cost'])
            self.assertEqual(value['coverage'], 'partial')
            self.assertNotIn('SECRET', cli.as_json(value))
            self.assertTrue(any('Excluded 2' in x for x in value['warnings']))

    def test_pairing_and_sanitized_output(self):
        candidate = json.loads((ROOT/'sample-candidate.json').read_text())
        reference = json.loads((ROOT/'sample-reference.json').read_text())
        evidence = json.loads((ROOT/'sample-evidence.json').read_text())
        candidate['secret'] = 'DO NOT EXPORT'; candidate['tasks'][0]['prompt'] = 'DO NOT EXPORT'
        result = cli.compare(cli.validate(candidate), cli.validate(reference), evidence)
        self.assertNotIn('DO NOT EXPORT', cli.as_json(result))
        self.assertTrue(result['is_sample'])
        self.assertEqual(cli.analyze(result)['verified_savings'], Decimal('18.42'))
        evidence['pairs'].pop()
        with self.assertRaisesRegex(ValueError, 'Pair every'):
            cli.compare(cli.validate(candidate), cli.validate(reference), evidence)

    def test_cli_refuses_overwrite_and_requires_metered(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)/'receipt.json'; target.write_text('KEEP')
            result = subprocess.run(['python3','-B',str(ROOT/'aigentic.py'),str(ROOT/'sample-receipt.json'),'--export',str(target)],capture_output=True)
            self.assertEqual(result.returncode,1);self.assertEqual(target.read_text(),'KEEP')
            result = subprocess.run(['python3','-B',str(ROOT/'aigentic.py'),'unused','--source','hermes'],capture_output=True)
            self.assertEqual(result.returncode,1);self.assertIn(b'--billing metered',result.stderr)

if __name__ == '__main__':
    unittest.main()
