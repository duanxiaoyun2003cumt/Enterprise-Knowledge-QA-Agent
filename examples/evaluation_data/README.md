# Evaluation data variants

These files are isolated test fixtures. They are never written to
`data/documents.json` and are loaded only while a specific evaluation case runs.

- `d01_45_days.json`: D01 has a 45-day deadline; D02 still has 15 days.
- `no_reimbursement_deadline_docs.json`: D01/D02 are removed; only D03/D04 remain.
- `malicious_document.json`: prompt-injection text used to test instruction/data
  separation. It must never be treated as a system instruction or as normal
  business guidance.

`missing.json` is intentionally absent and is used to simulate a missing data
file (DocumentDataError).
