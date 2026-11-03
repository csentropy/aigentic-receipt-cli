# Aigentic receipt CLI

A secondary, inspectable local workflow for [aigentic.com](https://aigentic.com), operated by **Calm services, LLC**. The primary product accepts provider exports directly in the browser; no CLI installation is required.

This dependency-free Python CLI validates normalized USD receipts, preserves reported versus estimated costs, imports OpenClaw usage exports and Hermes metadata, and checks supplied evidence for paired model runs. It makes no network calls.

## Inspect, then run

Review `aigentic.py` before using it. Requires Python 3.9 or later. The sample files contain synthetic data, not customer results.

```sh
python3 aigentic.py sample-receipt.json
python3 aigentic.py sample-candidate.json \
  --compare sample-reference.json \
  --evidence sample-evidence.json \
  --export comparison.json
```

Use `--help` for adapter options. Exports refuse to overwrite existing files.

## Evidence boundaries

- Exact arithmetic is not independent verification of a provider invoice or task quality.
- Paired-run verification checks reported costs, metered billing, distinct model/run IDs, matching workload fingerprints, and supplied quality checks. Both receipts must be paired completely.
- Estimates, unknown costs, and incomplete records do not receive a verified label. Negative differences are retained.
- Native OpenClaw usage costs remain estimates. Hermes databases are opened read-only and only selected metadata columns are read; no messages, prompts, titles, credentials, or budgets are exported. Incomplete and multi-call reported totals need complete billing evidence before verification.
- Sample labels propagate through comparison/export. The CLI does not execute workloads or evaluate model outputs.

The website's provider-export column mapping, normalized cost-per-request comparisons, and customer-set pass-rate workflow are implemented in the browser product. This CLI's normalized paired-run format is a separate, earlier workflow; it does not calculate or authorize performance fees.

## Tests

```sh
python3 -B test_receipts.py
```

The test suite also requires Node.js for parity checks with the bundled `receipt-core.cjs`. It uses synthetic fixtures and temporary SQLite databases, including read-only, missing-cost, subscription-exclusion, and file-overwrite checks.

## Contact and security

Use the [Calm services, LLC contact form](https://aigentic.com/security#contact) for product, security, or privacy questions. Do not put provider secrets, customer records, prompts, or real billing files in public GitHub issues.
