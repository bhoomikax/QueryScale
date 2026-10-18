# Measured ML experiment, 2026-10-18

This file records the first training run of the index-benefit model on **measured**
benchmark labels. It is a record of what was observed on one machine, not a claim about
general index quality. Every number below is copied from a saved file; frozen copies of
those files are committed under `docs/evidence/2026-10-18/` (the working copies in
`data/` and `models/` are gitignored and change on every run).

| Number in this document | Traceable to |
| --- | --- |
| Per-candidate benefits, index sizes, write probes | `docs/evidence/2026-10-18/benchmark_history.jsonl` (`summary.workload_benefit`, `index_size_bytes`, `write_overhead`), summarized in `benchmark_runs_summary.csv` |
| Training-set counts, CV and held-out metrics, ranking comparison | `docs/evidence/2026-10-18/models/evaluation.json` |
| Model version, selected algorithm, checksum, run IDs used | `docs/evidence/2026-10-18/models/index_ranker.json` |
| Leave-one-candidate-out metrics | `docs/evidence/2026-10-18/models/evaluation_logo.json` |
| Row counts | `docs/evidence/2026-10-18/schema_metadata.json` |
| Synthetic demo metrics (for contrast only) | `docs/evidence/2026-10-18/models/demo/evaluation.json` |

**Synthetic versus measured.** The model in `models/demo/` (2026-10-14) was trained on
labels produced by a formula and reports held-out MAE 0.023 against a baseline of 0.044.
Those numbers describe how well it learned the formula and say nothing about MySQL. They
are not comparable with anything below and must not be quoted as index-quality results.
The application refuses to rank with that model (`label_source: synthetic_demo`).

## Environment

| Item | Value |
| --- | --- |
| Machine | Windows 11 laptop, Docker Desktop, single MySQL instance, no concurrent load |
| MySQL | 8.4.11 in the `queryscale-mysql` Compose service (host port 3307) |
| Dataset | 50 branches, 10,000 customers, 15,000 accounts, 100,000 transactions, 5,000 loans (seed script defaults), dataset signature `73be471999e9e1bf` |
| Workload | 14 query templates from `scripts/workload.py`, 200 iterations, seed 42, 2 timed repeats after 1 warm-up, workload signature `ce72eafb9ed1603c` |
| Benchmark protocol | version 2 (`backend/services/benchmark.py`) |
| Python / scikit-learn | 3.13.15 / 1.9.1 (host), 3.12.15 / 1.9.1 (container) |

## Commands

```powershell
py scripts/run_local_pipeline.py --initialize-schema --iterations 200 --seed 42
py scripts/collect_benchmark_labels.py --limit 12 --iterations 200 --repeats 2   # run three times
py scripts/train_model.py
```

## Labels collected

Each row is one candidate; each candidate was benchmarked three times (three passes of
the collection script). `workload_benefit` is the share of total median-weighted workload
latency removed by the index, counting only query templates whose post-index EXPLAIN uses
it. Write overhead comes from 25 single-row INSERT/UPDATE/DELETE probes inside a
rolled-back transaction; with that few probes the percentages swing widely between runs
and should be read as "no large effect detected", not as precise costs.

| Candidate | Rule score | Runs | Mean benefit | Min | Max | Measured index size (MiB) |
| --- | --- | --- | --- | --- | --- | --- |
| IDX_TRANSACTIONS_TXN_TYPE_AMOUNT_ACCOUNT_ID | 50.6 | 3 | 34.51% | 32.97% | 35.99% | 4.52 |
| IDX_TRANSACTIONS_TXN_TYPE_AMOUNT | 45.5 | 3 | 31.85% | 30.18% | 33.95% | 3.52 |
| IDX_TRANSACTIONS_TXN_DATE_AMOUNT | 38.0 | 3 | 6.45% | 6.04% | 6.79% | 3.52 |
| IDX_LOANS_STATUS_PRINCIPAL | 31.4 | 3 | 1.32% | 0.96% | 1.73% | 0.17 |
| IDX_ACCOUNTS_BRANCH_ID_ACCOUNT_TYPE | 33.2 | 3 | 1.22% | 1.09% | 1.48% | 0.41 |
| IDX_ACCOUNTS_BRANCH_ID_ACCOUNT_TYPE_BALANCE | 37.5 | 3 | 1.16% | 1.12% | 1.22% | 1.52 |
| IDX_ACCOUNTS_BALANCE_CUSTOMER_ID | 45.4 | 3 | 0.72% | 0.69% | 0.77% | 0.47 |
| IDX_ACCOUNTS_BALANCE | 43.4 | 3 | 0.55% | 0.37% | 0.67% | 0.34 |
| IDX_CUSTOMERS_BRANCH_ID_KYC_STATUS | 35.3 | 3 | 0.32% | 0.25% | 0.42% | 0.28 |
| IDX_CUSTOMERS_BRANCH_ID_KYC_STATUS_NAME | 39.2 | 3 | 0.22% | 0.19% | 0.25% | 0.42 |
| IDX_LOANS_STATUS_CUSTOMER_ID_PRINCIPAL | 34.9 | 3 | 0.00% | 0.00% | 0.00% | 0.20 |
| IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE | 43.7 | 3 | -0.02% | -0.20% | 0.10% | 3.52 |

Observations:

- Repeated measurements of one candidate agree to within about one percentage point, so
  the label is stable on this machine.
- Two candidates give a large measured benefit; the rest give under 7%, and one is zero
  because the planner never chose it (no affected query). One is marginally negative.
- The rule score ranks `IDX_ACCOUNTS_BALANCE_CUSTOMER_ID` and `IDX_ACCOUNTS_BALANCE` second
  and fifth, but both measure under 1%. The rule score is a heuristic priority, and this
  is a concrete case where measurement disagrees with it.

Example per-query detail for the top candidate (run `BR-20261018T171247Z-ef1c84e9`):

| Query | Executions | Median before | Median after | Rows examined before | Rows examined after | Index used after |
| --- | --- | --- | --- | --- | --- | --- |
| Q011 (`txn_type = ? AND amount > ?` with account lookup) | 40 | 41.23 ms | 13.59 ms | 20,000 | 2,996 | candidate |
| Q002 (`txn_date BETWEEN`) | 34 | 13.65 ms | 13.54 ms | 1,554 | 1,554 | unchanged |
| Q001 (`account_id = ?`) | 70 | 1.63 ms | 1.73 ms | 7.3 | 7.3 | unchanged |

## Training set

| Item | Count |
| --- | --- |
| Records in benchmark history | 38 |
| Excluded: not COMPLETED | 1 (the 2026-10-14 `test_database_missing` failure) |
| Excluded: different workload signature | 1 (the single-repeat run from `run_local_pipeline.py`) |
| Records used | 36 |
| Distinct candidates (groups) | 12 |
| Required minimum | 20 records, 5 groups |
| Train / held-out split (grouped by candidate) | 27 rows from 9 candidates / 9 rows from 3 candidates |
| Label mean / std / min / max | 0.065 / 0.121 / -0.002 / 0.360 |

## Model selection and evaluation

Model selection used grouped 5-fold cross-validation on the 9 training candidates;
the held-out metrics are on the 3 candidates the model never saw.

| Model | CV MAE (train split) | Held-out MAE | Held-out RMSE |
| --- | --- | --- | --- |
| dummy_mean (baseline) | 0.0717 | 0.1112 | 0.1609 |
| ridge (**selected**) | 0.0516 | 0.0444 | 0.0618 |
| decision_tree | 0.0748 | 0.1037 | 0.1630 |
| random_forest | 0.0575 | 0.0288 | 0.0306 |
| gradient_boosting | 0.0592 | 0.0270 | 0.0350 |

Selected model: `ridge`, version `v20261018T172058Z-e813dcc956e5`, chosen because it had
the lowest grouped CV MAE on the training split. Suitability: held-out MAE 0.0444 beats
the mean baseline 0.1112, so the model is marked suitable and the recommender uses it.

Ranking comparison on the held-out rows (one dataset group, k = 5, relevance threshold
5% benefit):

| Ranking | NDCG@5 | Precision@5 | Spearman |
| --- | --- | --- | --- |
| Rule score | 0.932 | 0.60 | 0.474 |
| ML prediction | 1.000 | 1.00 | 0.949 |

## Leave-one-candidate-out check (added the same day)

Three held-out candidates cannot support a general claim, so
`scripts/evaluate_leave_one_out.py` re-evaluated the same 36 records by holding out each
candidate in turn (12 folds). Every candidate's prediction therefore comes from a model
that never saw any of its runs. The script trains nothing for production; the ridge model
above remains the deployed one. "Per candidate" averages the three repeats of each
candidate first, so the ranking metrics are over 12 candidates.

| Model | MAE per candidate | Spearman | NDCG@5 | Precision@5 |
| --- | --- | --- | --- | --- |
| dummy_mean (baseline) | 0.097 | -1.00 | 0.008 | 0.0 |
| ridge (deployed) | 0.039 | 0.29 | 0.966 | 0.6 |
| decision_tree | 0.015 | 0.07 | 0.915 | 0.4 |
| random_forest | 0.027 | 0.47 | 0.966 | 0.6 |
| gradient_boosting | 0.018 | 0.33 | 0.958 | 0.6 |
| rule score (no training) | n/a | 0.21 | 0.937 | 0.4 |

How to read this honestly:

- Only 3 of the 12 candidates have a benefit of at least 5%, so **0.6 is the maximum
  possible Precision@5**. Ridge, random forest, and gradient boosting all reach that
  ceiling out-of-fold: every candidate worth recommending lands in their top five. The
  rule score places two of the three there.
- The Spearman values are low for every method because nine candidates sit between 0%
  and 1.3% benefit, where the differences are within measurement noise. No method can
  order those reliably, and the metric mostly reflects that noise.
- Predicting the *size* of the benefit is where the models clearly beat the mean baseline
  (MAE 0.015 to 0.039 versus 0.097). Tree models predict magnitudes better than ridge on
  this data; the project's selection rule (lowest grouped CV MAE on the training split)
  chose ridge, and with 9 training candidates that choice is not robust.
- The supportable claim is therefore: *on this workload and dataset, the models separate
  high-benefit from negligible candidates out-of-fold and estimate the benefit magnitude
  far better than a mean predictor; their ranking advantage over the rule score is small.*
  No claim about other workloads or databases is supported.

## Limitations (read before quoting any number)

1. **Small sample.** 12 distinct candidates, 3 held out in the training report. The
   held-out set has 9 rows but only 3 independent candidates; the three repeats of each
   candidate are near-identical, so the ranking metrics in the training report are
   computed over effectively 3 items. The leave-one-out section above is the better
   summary of out-of-sample behaviour, and it still covers only 12 candidates.
2. **One workload, one dataset, one machine.** All labels come from the same 14 templates
   on the same seeded data without concurrent load. The model has not been tested on a
   different workload or database and may not transfer.
3. **Dominated by two large-benefit candidates.** Most of the MAE improvement over the
   baseline comes from predicting the two Transactions candidates correctly.
4. **Reranked recommendations are in-sample.** The dashboard now orders the same 12
   candidates the model was trained on; agreement between predictions and labels there is
   expected and is not evidence of generalization.
5. **Write overhead is noisy** (25 single-row probes, rolled back, commit cost excluded).
6. Random forest and gradient boosting had lower held-out MAE than ridge but higher CV
   MAE; with 9 training candidates the CV estimate is itself unstable. Selection by CV is
   the project's protocol and was applied as written.
7. **Exact re-seeding is not bit-for-bit.** `scripts/seed.py` had no random seed when
   this data was generated. Re-running the pipeline gives the same row counts (same
   dataset signature) but different row values and somewhat different timings. A `--seed`
   option was added afterwards for future runs. Training from the saved history file is
   deterministic (`random_state=42`).

A credible next step is to repeat the collection on at least one other dataset size and
one other workload mix, so the model is evaluated on candidates and workloads it has not
seen.
