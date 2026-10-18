# Evidence snapshot, 2026-10-18

Frozen copies of the generated files behind `docs/ml-experiment-2026-10-18.md`. The
working copies under `data/` and `models/` are gitignored because they change on every
run; this folder is the version that the report numbers were read from. Nothing here
contains credentials: the files hold query templates, timings, EXPLAIN plans, row counts,
and model metrics. Model binaries (`*.joblib`, Python pickles) are deliberately excluded;
the model is re-created from `benchmark_history.jsonl` by `scripts/train_model.py`.

| File | Source | What it proves |
| --- | --- | --- |
| `benchmark_history.jsonl` | `data/benchmark_history.jsonl` | All 39 protocol-v2 benchmark records: 38 COMPLETED, 1 FAILED (2026-10-14, `test_database_missing`). Each record carries the candidate feature snapshot, workload signature, dataset signature, per-query timings, EXPLAIN plans, index size, write probes, label, and cleanup status. |
| `benchmark_runs_summary.csv` | derived from the file above | One row per run for reading in a spreadsheet. |
| `schema_metadata.json` | `data/schema_metadata.json` | Live schema and exact row counts of the seeded database (5 tables, 130,050 rows). |
| `query_features.csv` | `data/query_features.csv` | Per-template workload statistics from the 200-iteration log. |
| `models/evaluation.json` | `models/evaluation.json` | Training report: 36 records, 12 candidates, grouped split, CV and held-out metrics, ranking comparison, limitations. |
| `models/index_ranker.json` | `models/index_ranker.json` | Metadata of the production model `v20261018T172058Z-e813dcc956e5` (ridge, `label_source: measured_benchmark`, checksum, provenance run IDs). |
| `models/evaluation_logo.json` | `scripts/evaluate_leave_one_out.py` | Leave-one-candidate-out diagnostic over the same 36 records. |
| `models/demo/*.json` | `models/demo/` | The **synthetic** demonstration model from 2026-10-14. Its metrics describe a formula, not MySQL. Kept only to show the distinction. |
| `models/archive-20261014-insufficient-data/evaluation.json` | `models/archive/` | The 2026-10-14 training attempt that found 0 measured records. |
| `legacy-main-2026-10-05/*` | `data_backup/legacy-main-2026-10-05/` | The pre-protocol-v2 files from the `main` branch: the Oct 5 query log, the old-format benchmark CSV (7 rows, one candidate, no history record), and the single failed history record. Not used for training. |

Reproduce from this folder without a database:

```powershell
py scripts/train_model.py                                  # reads data/benchmark_history.jsonl
py scripts/evaluate_leave_one_out.py --history docs/evidence/2026-10-18/benchmark_history.jsonl --output models/evaluation_logo_check.json
```

Note on exact reproduction: `scripts/seed.py` did not take a random seed when this data
was generated, so re-seeding produces a database with the same row counts but different
values and therefore slightly different timings. Training from this folder's history file
is deterministic (`random_state=42`); re-collecting the labels is not bit-for-bit.
