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
