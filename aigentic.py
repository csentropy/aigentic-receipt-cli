#!/usr/bin/env python3
"""Aigentic v0.2: local OpenClaw/Hermes receipts and evidence-checked comparisons.
Python 3.9+, standard library only. No network requests. Hermes is opened read-only;
messages, prompts, titles, credentials, and budgets are never selected.
"""
import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

BASES = {'reported', 'estimated', 'unknown', 'incomplete'}
CATEGORIES = {'coding', 'research', 'background'}
MONEY_LIMIT = Decimal('100000000')


def short_text(value, maximum=150):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def amount(value, label):
    if value is None:
        return None
    if type(value) not in (int, float, Decimal) or not math.isfinite(value) or not 0 <= value <= MONEY_LIMIT:
        raise ValueError(f'{label} must be a finite nonnegative USD amount, or null when unknown.')
    return Decimal(str(value))


def clean_proof(proof, label):
    if not isinstance(proof, dict):
        raise ValueError(f'{label}: comparison must be an object.')
    result = {}
    for field in ('reference_task_id', 'workload_sha256', 'reference_workload_sha256', 'evaluator'):
        if field in proof:
            if not short_text(proof[field]):
                raise ValueError(f'{label}: invalid {field}.')
            result[field] = proof[field].strip()
    if 'checks' in proof:
        if not isinstance(proof['checks'], list) or len(proof['checks']) > 100:
            raise ValueError(f'{label}: supply at most 100 quality checks.')
        checks, seen = [], set()
        for check in proof['checks']:
            if (not isinstance(check, dict) or not short_text(check.get('name'), 80)
                    or type(check.get('candidate_passed')) is not bool
                    or type(check.get('reference_passed')) is not bool):
                raise ValueError(f'{label}: every check needs a name and two boolean results.')
            name = check['name'].strip()
            if name in seen:
                raise ValueError(f'{label}: quality-check names must be unique.')
            seen.add(name)
            checks.append({'name': name, 'candidate_passed': check['candidate_passed'],
                           'reference_passed': check['reference_passed']})
        result['checks'] = checks
    return result


def validate(value):
    if not isinstance(value, dict) or value.get('currency') != 'USD':
        raise ValueError('Use a receipt JSON object with currency set to USD.')
    rows = value.get('tasks')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError('Include between 1 and 5,000 tasks.')
    clean, ids = [], set()
    for i, row in enumerate(rows, 1):
        label = f'Task {i}'
        if not isinstance(row, dict) or not short_text(row.get('name')):
            raise ValueError(f'{label} needs a name of 1–150 characters.')
        if row.get('category') not in CATEGORIES:
            raise ValueError(f'{label}: category must be coding, research, or background.')
        calls = row.get('calls', 'missing')
        if calls is not None and (type(calls) not in (int, float) or not math.isfinite(calls)
                                  or int(calls) != calls or not 0 <= calls <= 1_000_000_000):
            raise ValueError(f'{label}: calls must be a nonnegative whole number or null.')
        row_id = row.get('id', f'task-{i}')
        if not short_text(row_id) or row_id.strip() in ids:
            raise ValueError(f'{label}: task IDs must be nonempty and unique.')
        ids.add(row_id.strip())
        item = {'id': row_id.strip(), 'name': row['name'].strip(), 'category': row['category'],
                'calls': int(calls) if calls is not None else None}
        if 'cost' not in row:
            raise ValueError(f'{label}: cost is required; use null when unknown.')
        for key in ('cost', 'reference_cost', 'alternative_cost'):
            if key in row:
                item[key] = amount(row[key], f'{label}: {key}')
        for key in ('cost_basis', 'reference_cost_basis'):
            item[key] = row.get(key, 'unknown')
            if item[key] not in BASES:
                raise ValueError(f'{label}: invalid {key}.')
        for key in ('model', 'reference_model'):
            if key in row:
                if not short_text(row[key]):
                    raise ValueError(f'{label}: invalid {key}.')
                item[key] = row[key].strip()
        if 'comparison' in row:
            item['comparison'] = clean_proof(row['comparison'], label)
        clean.append(item)
    billing, source = value.get('billing', 'unknown'), value.get('source', 'normalized')
    if billing not in ('metered', 'subscription', 'unknown'):
        raise ValueError('billing must be metered, subscription, or unknown.')
    if source not in ('openclaw', 'hermes', 'normalized'):
        raise ValueError('Unsupported receipt source.')
    warnings = value.get('warnings', [])
    return {'schema_version': 2, 'currency': 'USD', 'source': source, 'billing': billing,
            'is_sample': value.get('is_sample') is True,
            'coverage': 'partial' if value.get('coverage') == 'partial' else 'complete',
            'warnings': [x for x in warnings if short_text(x, 240)][:10] if isinstance(warnings, list) else [],
            'tasks': clean}


def evidence(row, receipt):
    proof, reasons = row.get('comparison', {}), []
    if receipt['billing'] != 'metered':
        reasons.append('Metered billing not confirmed')
    if row.get('cost') is None or row.get('reference_cost') is None:
        reasons.append('Both costs are required')
    if row['cost_basis'] != 'reported' or row['reference_cost_basis'] != 'reported':
        reasons.append('Both costs must be reported, not estimated')
    if (not row.get('model') or not row.get('reference_model') or row['model'] == row['reference_model']
            or row.get('model', '').lower() in ('unknown', 'mixed') or row.get('reference_model', '').lower() in ('unknown', 'mixed')):
        reasons.append('Two different known model routes are required')
    if not proof.get('reference_task_id') or any(task['id'] == proof['reference_task_id'] for task in receipt['tasks']):
        reasons.append('A distinct reference run is required')
    if proof.get('reference_task_id') and sum(task.get('comparison', {}).get('reference_task_id') == proof['reference_task_id'] for task in receipt['tasks']) > 1:
        reasons.append('A reference run cannot be counted twice')
    fingerprint = proof.get('workload_sha256', '')
    if not re.fullmatch(r'[a-f0-9]{64}', fingerprint) or fingerprint != proof.get('reference_workload_sha256'):
        reasons.append('Workload fingerprints must match')
    checks = proof.get('checks', [])
    if not proof.get('evaluator') or not checks or not all(c['candidate_passed'] and c['reference_passed'] for c in checks):
        reasons.append('Both runs must pass the supplied quality checks')
    return {'passed': not reasons, 'reasons': reasons, 'checks': len(checks)}


def analyze(receipt):
    rows = receipt['tasks']
    cost = sum((r['cost'] or Decimal(0) for r in rows), Decimal(0))
    has_costs = all(r['cost'] is not None for r in rows)
    has_reference = has_costs and all(r.get('reference_cost') is not None for r in rows)
    complete = has_costs and receipt['coverage'] == 'complete' and all(r['cost_basis'] != 'incomplete' for r in rows)
    reference = sum((r['reference_cost'] for r in rows), Decimal(0)) if has_reference else None
    delta = reference - cost if reference is not None else None
    checked = [evidence(row, receipt) for row in rows]
    distinct_references = len({r.get('comparison', {}).get('reference_task_id') for r in rows}) == len(rows)
    verified = complete and distinct_references and all(c['passed'] for c in checked)
    opportunity = (sum((r['cost'] - r['alternative_cost'] for r in rows), Decimal(0))
                   if complete and all(r.get('alternative_cost') is not None for r in rows) else None)
    percent = int((delta / reference * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)) if reference and delta is not None else None
    return {'currency': 'USD', 'source': receipt['source'], 'is_sample': receipt['is_sample'],
            'tasks': len(rows), 'calls': sum(r['calls'] or 0 for r in rows),
            'calls_complete': all(r['calls'] is not None for r in rows), 'cost': cost,
            'cost_coverage_complete': complete, 'reference_cost': reference,
            'recorded_savings_vs_supplied_reference': delta,
            'verified_savings': delta if verified else None, 'receipt_checks_passed': verified,
            'estimated_additional_savings': opportunity, 'comparison_reduction_percent': percent,
            'comparison_kind': 'verified' if verified else 'unverified' if has_reference else 'unavailable',
            'warnings': receipt['warnings'],
            'verification': [{'id': row['id'], **check} for row, check in zip(rows, checked)]}


def read_json(path, limit=16 * 1024 * 1024):
    with path.open('rb') as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('JSON input exceeds 16 MB. Export a smaller date range.')
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError('Input is not valid UTF-8 JSON.') from error


def private_id(source, value):
    return source + '-' + hashlib.sha256(str(value).encode()).hexdigest()[:20]


def openclaw_receipt(value, category):
    if isinstance(value, dict) and isinstance(value.get('result'), dict):
        value = value['result']
    if not isinstance(value, dict) or not isinstance(value.get('sessions'), list):
        raise ValueError('Expected an OpenClaw sessions.usage JSON export containing a sessions array.')
    if not 1 <= len(value['sessions']) <= 5000:
        raise ValueError('Export between 1 and 5,000 OpenClaw sessions.')
    cache = value.get('cacheStatus') or {}
    if not isinstance(cache, dict):
        raise ValueError('OpenClaw cacheStatus must be an object.')
    partial = bool(value.get('partial')) or cache.get('status') in ('partial', 'stale', 'refreshing')
    rows = []
    for i, session in enumerate(value['sessions'], 1):
        if not isinstance(session, dict):
            raise ValueError('Every OpenClaw session must be an object.')
        usage = session.get('usage') or {}
        if not isinstance(usage, dict):
            raise ValueError('OpenClaw usage metadata must be an object.')
        cost = usage.get('totalCost')
        models = usage.get('modelUsage') or []
        if not isinstance(models, list) or not all(isinstance(m, dict) for m in models):
            raise ValueError('OpenClaw modelUsage must be an array of objects.')
        names = {m.get('model') for m in models if short_text(m.get('model'))}
        model = next(iter(names)) if len(names) == 1 else 'mixed' if len(names) > 1 else session.get('model') or 'unknown'
        calls = sum(m['count'] for m in models) if models and all(type(m.get('count')) is int and m['count'] >= 0 for m in models) else None
        incomplete = partial or bool(usage.get('missingCostEntries')) or bool(session.get('computing')) or bool(usage.get('refreshing'))
        basis = 'unknown' if cost is None else 'incomplete' if incomplete else 'estimated'
        rows.append({'id': private_id('openclaw', session.get('key') or session.get('sessionId') or f'row-{i}'),
                     'name': f'OpenClaw session {i}', 'category': category, 'calls': calls,
                     'cost': cost, 'cost_basis': basis, 'model': model})
    return validate({'currency': 'USD', 'source': 'openclaw', 'billing': 'metered',
                     'coverage': 'partial' if partial or any(r['cost_basis'] in ('unknown', 'incomplete') for r in rows) else 'complete',
                     'warnings': ['OpenClaw usage costs may use local pricing; imported amounts remain estimates, not provider invoices.'], 'tasks': rows})


def hermes_receipt(path, category, days):
    if not path.is_file():
        raise ValueError('Hermes database not found. Pass the state.db path for the profile you use.')
    fields = ('id', 'model', 'started_at', 'ended_at', 'parent_session_id', 'billing_mode',
              'estimated_cost_usd', 'actual_cost_usd', 'cost_status', 'api_call_count')
    conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        table = conn.execute("SELECT type FROM sqlite_master WHERE name='sessions'").fetchone()
        if table is None or table[0] != 'table':
            raise ValueError('This is not a supported Hermes sessions database.')
        columns = {r[1] for r in conn.execute('PRAGMA table_info(sessions)')}
        if not {'id', 'model', 'started_at'}.issubset(columns):
            raise ValueError('Hermes sessions schema is missing required metadata columns.')
        selected = [field for field in fields if field in columns]
        where = 'started_at >= ?' + (' AND parent_session_id IS NULL' if 'parent_session_id' in columns else '')
        sql = 'SELECT ' + ', '.join('"' + field + '"' for field in selected) + ' FROM sessions WHERE ' + where + ' ORDER BY started_at DESC LIMIT 5001'
        records = [dict(r) for r in conn.execute(sql, (time.time() - days * 86400,))]
    finally:
        conn.close()
    if len(records) > 5000:
        raise ValueError('More than 5,000 Hermes sessions found. Reduce --days.')
    rows, skipped = [], 0
    for record in records:
        mode = str(record.get('billing_mode') or '').lower()
        if record.get('cost_status') == 'included' or any(word in mode for word in ('subscription', 'oauth', 'flat_rate', 'flat-rate', 'included')):
            skipped += 1
            continue
        actual, estimated = record.get('actual_cost_usd'), record.get('estimated_cost_usd')
        status = str(record.get('cost_status') or '').lower()
        reported = actual is not None and status == 'actual'
        cost = actual if reported else estimated if estimated is not None else actual
        basis = 'reported' if reported else 'estimated' if estimated is not None else 'unknown'
        if status in ('unknown', 'unavailable') or (not reported and cost == 0 and status != 'estimated'):
            cost, basis = None, 'unknown'
        # The session aggregate stores the last status and may contain only a
        # subset of actual charges. It cannot prove all calls were billed.
        if cost is not None and (status in ('partial', 'incomplete') or record.get('ended_at') is None
                                 or (reported and record.get('api_call_count') != 1)):
            basis = 'incomplete'
        if cost is None:
            basis = 'unknown'
        rows.append({'id': private_id('hermes', record['id']), 'name': f'Hermes session {len(rows)+1}',
                     'category': category, 'calls': record.get('api_call_count'), 'cost': cost,
                     'cost_basis': basis, 'model': record.get('model') or 'unknown'})
    if not rows:
        raise ValueError('No metered-compatible Hermes sessions found in this date range.')
    warnings = [f'Imported top-level sessions from the selected Hermes profile for the last {days} days.']
    if any(r['cost_basis'] == 'incomplete' for r in rows):
        warnings.append('Multi-call reported totals or unfinished sessions need complete billing evidence before verification; native session status alone is insufficient.')
    if skipped:
        warnings.append(f'Excluded {skipped} sessions marked as subscription or OAuth billing.')
    if any(r['cost_basis'] in ('unknown', 'incomplete') for r in rows):
        warnings.append('Some session costs are unavailable or incomplete; the known-cost subtotal is not a complete bill.')
    return validate({'currency': 'USD', 'source': 'hermes', 'billing': 'metered', 'warnings': warnings,
                     'coverage': 'partial' if any(r['cost_basis'] in ('unknown', 'incomplete') for r in rows) else 'complete', 'tasks': rows})


def compare(candidate, reference, evidence_file):
    if candidate['billing'] != 'metered' or reference['billing'] != 'metered':
        raise ValueError('Model comparisons require both receipts to declare metered billing.')
    if candidate['source'] != reference['source']:
        raise ValueError('Compare receipts from the same agent source to keep runtime scope consistent.')
    if not isinstance(evidence_file, dict) or not isinstance(evidence_file.get('pairs'), list):
        raise ValueError('Evidence must contain a pairs array. See the downloadable example.')
    refs = {row['id']: row for row in reference['tasks']}
    candidates = {row['id']: row for row in candidate['tasks']}
    used_refs, used_candidates = set(), set()
    for pair in evidence_file['pairs']:
        if not isinstance(pair, dict):
            raise ValueError('Every evidence pair must be an object.')
        candidate_id, reference_id = pair.get('candidate_id'), pair.get('reference_id')
        if candidate_id not in candidates or reference_id not in refs:
            raise ValueError('Evidence references a task ID that is absent from its receipt.')
        if candidate_id in used_candidates or reference_id in used_refs:
            raise ValueError('Each candidate and reference run can appear in only one pair.')
        used_candidates.add(candidate_id)
        used_refs.add(reference_id)
        row, before = candidates[candidate_id], refs[reference_id]
        if row['category'] != before['category']:
            raise ValueError('Matched tasks must use the same category.')
        row.update(reference_cost=before['cost'], reference_cost_basis=before['cost_basis'],
                   reference_model=before.get('model', 'unknown'))
        proof = {key: pair[key] for key in ('workload_sha256', 'reference_workload_sha256', 'evaluator', 'checks') if key in pair}
        proof['reference_task_id'] = reference_id
        row['comparison'] = clean_proof(proof, row['id'])
    if used_candidates != set(candidates) or used_refs != set(refs):
        raise ValueError('Pair every candidate and reference run exactly once; export matched scopes before comparing.')
    candidate['is_sample'] = candidate['is_sample'] or reference['is_sample'] or evidence_file.get('is_sample') is True
    if reference['coverage'] == 'partial':
        candidate['coverage'] = 'partial'
    return candidate


def dollars(value):
    return '$' + f'{value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP):,.2f}' if value is not None else 'Not available'


def as_json(value):
    return json.dumps(value, default=lambda number: float(number), indent=2, allow_nan=False)


def main():
    parser = argparse.ArgumentParser(description='OpenClaw / Hermes receipts and evidence-checked model comparisons. Local only.')
    parser.add_argument('receipt', type=Path, help='Normalized receipt, OpenClaw usage JSON, or Hermes state.db')
    parser.add_argument('--source', choices=('normalized', 'openclaw', 'hermes'), default='normalized')
    parser.add_argument('--billing', choices=('metered',), help='Confirm metered API billing (required for native adapters)')
    parser.add_argument('--category', choices=sorted(CATEGORIES), default='background', help='Task classification for the imported workload')
    parser.add_argument('--days', type=int, default=7, help='Hermes lookback window, 1–3650 days; default 7')
    parser.add_argument('--compare', type=Path, help='Reference normalized receipt for a paired model comparison')
    parser.add_argument('--evidence', type=Path, help='Pair mapping, workload fingerprints, and supplied quality-check results')
    parser.add_argument('--export', metavar='PATH', help='Write a sanitized normalized receipt; - for stdout. Existing files are preserved.')
    parser.add_argument('--json', action='store_true', help='Print a machine-readable analysis summary')
    args = parser.parse_args()
    try:
        if not 1 <= args.days <= 3650:
            raise ValueError('--days must be between 1 and 3650.')
        if args.source != 'normalized' and args.billing != 'metered':
            raise ValueError('Native adapters require --billing metered. Subscription quotas are not per-task savings.')
        if args.source == 'hermes':
            receipt = hermes_receipt(args.receipt.expanduser(), args.category, args.days)
        elif args.source == 'openclaw':
            receipt = openclaw_receipt(read_json(args.receipt.expanduser()), args.category)
        else:
            receipt = validate(read_json(args.receipt.expanduser()))
        if bool(args.compare) != bool(args.evidence):
            raise ValueError('Use --compare and --evidence together.')
        if args.compare:
            receipt = compare(receipt, validate(read_json(args.compare.expanduser())), read_json(args.evidence.expanduser()))
        result = analyze(receipt)
        if args.export:
            payload = as_json(receipt) + '\n'
            if args.export == '-':
                print(payload, end='')
                return 0
            with Path(args.export).expanduser().open('x', encoding='utf-8') as target:
                target.write(payload)
        if args.json:
            print(as_json(result))
        else:
            print('\n  aigentic. / CHEAPER MODELS. KEEP THE PROOF.\n  ' + '-' * 49)
            if receipt['is_sample']:
                print('  ILLUSTRATIVE SAMPLE — not a real customer receipt')
            print(f'  Source: {receipt["source"]}   Tasks: {result["tasks"]:,}')
            print(f'  Known candidate cost:         {dollars(result["cost"])}' + ('' if result['cost_coverage_complete'] else ' (PARTIAL)'))
            print(f'  Supplied reference cost:      {dollars(result["reference_cost"])}')
            print(f'  Cost difference:              {dollars(result["recorded_savings_vs_supplied_reference"])}')
            print(f'  Verified against evidence:    {dollars(result["verified_savings"])}')
            print(f'  Additional opportunity (EST): {dollars(result["estimated_additional_savings"])}')
            print('  Evidence checks:              ' + ('PASSED' if result['receipt_checks_passed'] else 'NOT VERIFIED'))
            for warning in receipt['warnings']:
                print('  Note: ' + warning)
            print('  ' + '-' * 49)
            print('  Verification checks supplied receipts and attestations.\n  Provider billing and quality tests are not independently audited.\n  Processed locally. Nothing uploaded.\n')
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, sqlite3.Error) as error:
        print(f'aigentic: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
