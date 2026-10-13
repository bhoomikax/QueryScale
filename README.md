# QueryScale

QueryScale is an intelligent database performance optimization system for banking workloads.

## Local requirements

- Python 3.11 or newer
- MySQL 8.x, MariaDB, or Docker Desktop
- PowerShell on Windows, or an equivalent shell

Install Python dependencies from the repository root:

```powershell
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

The example file contains local-development values only. `DB_TLS_VERIFY` is explicitly
disabled there to work with the self-signed TLS setup commonly used by local MySQL
containers. `backend.config.Settings` enables certificate and hostname verification by
default when that option is omitted. Keep verification enabled for deployed databases;
set `DB_TLS_CA` to the trusted CA bundle path when the database requires a specific CA.

Connection and read/write timeouts are configurable with `DB_CONNECTION_TIMEOUT`,
`DB_READ_TIMEOUT`, and `DB_WRITE_TIMEOUT`. The API bind address and port use `API_HOST`
and `API_PORT` (defaults `0.0.0.0` and `8000`).

## Run the API in Docker

Build the image from the repository root:

```powershell
docker build -t queryscale:local .
```

For a MySQL server running on the host, copy `.env.example` to `.env`, set its local
database name and credentials, and start the API container:

```powershell
Copy-Item .env.example .env
docker run --rm --name queryscale-api --env-file .env `
    -e DB_HOST=host.docker.internal -p 8000:8000 queryscale:local
```

For MySQL also running in Docker, put both containers on the same user-defined Docker
network and set `DB_HOST` to the MySQL container name instead. The dashboard is at
<http://127.0.0.1:8000/dashboard/>. Check process liveness at `/health/live` and database
readiness at `/health/ready`; the existing `/health` response remains available.

The image includes the repository's current `data/` CSV artifacts as a starting snapshot.
The benchmark endpoint can write `BENCHMARK_RESULT_PATH` inside the container, but that
output is ephemeral when the container is removed. Mount a writable volume at the result
path if you need to keep local Docker benchmark output. This phase does not make the CSV
persistence distributed or durable.

### CSV persistence work for the asynchronous cloud phase

These CSV paths are environment-configurable but still assume a local filesystem:

- `QUERY_LOG_PATH`: read by `backend/routes/queries.py`, produced by `scripts/workload.py`.
- `RECOMMENDATION_PATH`: read by `backend/routes/recommendations.py`, produced by the
  rule-based pipeline (`backend/services/recommender.py`).
- `BENCHMARK_RESULT_PATH`: read by `backend/routes/benchmark.py` and overwritten by
  `backend/services/benchmark.py`.
- Feature and candidate CSVs are intermediate pipeline files in
  `backend/services/feature_extractor.py`, `candidate_generator.py`, `recommender.py`,
  `backend/services/cli.py`, and `scripts/run_optimization.py`.

For asynchronous cloud processing, job state and dashboard-visible results must move to
shared durable storage; report exports can go to S3. Worker inputs and intermediate
artifacts must not rely on one task's local disk. The API will need to retrieve results
through that durable store, and the dashboard will need job submission/status behavior.
Those persistence and async changes are intentionally deferred from this container phase.

## MySQL setup

The application expects MySQL at `127.0.0.1:3306`. With Docker Desktop, start a local server:

```powershell
docker run --name queryscale-mysql `
    -e MYSQL_ROOT_PASSWORD=your_password `
    -e MYSQL_DATABASE=queryscale `
    -p 3306:3306 `
    -d mysql:8.4
```

Wait until the container log says `ready for connections`, then set the matching values in `.env`.
The complete pipeline can initialize the schema itself. The `--initialize-schema` flag resets the local source schema, so use it only for a disposable development database.

## Complete local demo

Run the full local pipeline from MySQL to the dashboard:

```powershell
py scripts/run_local_pipeline.py --initialize-schema --iterations 100 --serve
```

This performs the following stages:

1. Initializes `queryscale` from `database/schema.sql`.
2. Resets and seeds synthetic banking data.
3. Executes the real workload and records timings.
4. Extracts features and generates index recommendations.
5. Rebuilds the isolated `queryscale_test` database.
6. Applies the selected recommendation only to `queryscale_test`.
7. Warms up and runs before/after benchmarks, then writes measured results.
8. Starts the API and dashboard when `--serve` is provided.

For a smaller local run, keep enough workload iterations to sample recommendation candidates:

```powershell
py scripts/run_local_pipeline.py --initialize-schema --iterations 100 `
    --customers 100 --accounts 150 --transactions 500 --loans 50 --serve
```

Open the dashboard at <http://127.0.0.1:8000/dashboard/>.

## Individual workflow

Use these commands when running stages separately:

```powershell
py scripts/seed.py
py scripts/workload.py --iterations 100
py -m backend.services.test_database
py scripts/run_optimization.py --iterations 100
py -m backend.app
```

The analysis pipeline can also be run directly through the existing CLI:

```powershell
py -m backend.services.cli --log-path data/query_logs.csv
```

The workload and analysis stages produce these generated files:

- `data/query_logs.csv`
- `data/candidate_features.csv`
- `data/index_candidates.csv`
- `data/recommendations.csv`
- `data/benchmark_results.csv`

These files contain local measurements and should not be staged unless they are intentionally being shared as a fixture.

## API

Start the API with `py -m backend.app`. Available endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /health` | Application health check |
| `GET /api/queries` | Measured query log rows |
| `GET /api/queries/slow` | Queries above a latency threshold |
| `GET /api/recommendations` | Generated index recommendations |
| `GET /api/recommendations/{id}` | One recommendation by candidate ID |
| `POST /api/benchmark/{id}` | Apply and benchmark a recommendation |
| `GET /api/benchmark/results` | Persisted benchmark measurements |

The APIs read generated project data and never substitute hardcoded dashboard metrics.

## Dashboard

The dashboard is served by FastAPI at `/dashboard/`. It displays:

- Query, slow-query, recommendation, and priority counts
- Top recommendation cards
- Before/after benchmark comparisons weighted by measured query executions
- Recommendation score bars
- Query frequency and measured latency

It shows explicit empty or unavailable states until the corresponding local artifacts exist.

## Tests

Run the complete test suite without requiring MySQL:

```powershell
py -m unittest discover -s tests -p "test_*.py"
```

The live pipeline requires MySQL and should be run separately with the complete demo command above.

## Ownership and future work

### Person A: database and query intelligence

- MySQL schema and synthetic banking data
- Workload query definitions and query logs
- Query analysis and feature extraction
- Candidate generation and rule-based recommendations

### Person B: benchmark, API, and dashboard

- Isolated test database management
- Safe index application
- Before/after benchmark engine
- FastAPI routes and local dashboard
- End-to-end local integration and tests

### Later integration points

The generated feature and recommendation datasets are the input boundary for future ML experiments. The local FastAPI application and generated benchmark artifacts are the boundary for future cloud deployment. AWS and ML integration are intentionally out of scope for the current local milestone.

## Repository structure

```text
QueryScale/
├── backend/
│   ├── app.py
│   ├── config.py
│   ├── database.py
│   ├── routes/
│   └── services/
├── dashboard/
│   ├── dashboard.js
│   ├── index.html
│   └── style.css
├── database/
│   ├── README.md
│   ├── queries.sql
│   └── schema.sql
├── scripts/
│   ├── run_local_pipeline.py
│   ├── run_optimization.py
│   ├── seed.py
│   └── workload.py
├── tests/
├── .env.example
└── requirements.txt
```
