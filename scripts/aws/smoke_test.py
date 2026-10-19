#!/usr/bin/env python3
"""End-to-end smoke test for a deployed (or local) QueryScale API.

Submits one benchmark job through the public API, polls it to a terminal state, checks
the persisted run record and the cleanup status, and reports the ranking method. Works
against the ALB URL in AWS or http://127.0.0.1:8000 locally; only the standard library
is used so it runs anywhere Python 3.11+ is installed.

API contract (this branch): POST /api/benchmark/{id}?background=true returns 202 with a
job whose status moves PENDING -> RUNNING -> COMPLETED | FAILED at /api/jobs/{job_id}.
A completed job carries result.run_id, result.summary, and result.cleanup; the full run
record (per-query rows, EXPLAIN plans, cleanup) is at /api/benchmark/history/{run_id}.

Each check ends in one of three states:

  PASS         behaviour verified
  FAIL         behaviour wrong; exit code 1
  PENDING-APP  depends on an application feature that is not implemented yet
               (per-job S3 artifacts and a download endpoint). Exit code 1 unless
               --tolerate-missing-adapters is given, so a deployment review cannot
               mistake a pending feature for a passing test.

Examples:
  python scripts/aws/smoke_test.py --base-url http://queryscale-dev-api-123.us-east-1.elb.amazonaws.com
  python scripts/aws/smoke_test.py --base-url http://127.0.0.1:8000 --expect-local-artifacts --tolerate-missing-adapters
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


TERMINAL = {"COMPLETED", "FAILED"}


@dataclass
class Check:
    name: str
    state: str  # PASS | FAIL | PENDING-APP
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, state: str, detail: str = "") -> None:
        self.checks.append(Check(name, state, detail))
        print(f"[{state:<11}] {name}: {detail}", flush=True)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.state == "FAIL"]

    def pending(self) -> list[Check]:
        return [c for c in self.checks if c.state == "PENDING-APP"]


class Api:
    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, absolute: bool = False) -> tuple[int, bytes, dict[str, str]]:
        url = path if absolute else f"{self.base_url}{path}"
        request = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def json(self, method: str, path: str) -> tuple[int, object]:
        status, body, _ = self.request(method, path)
        try:
            return status, json.loads(body) if body else None
        except json.JSONDecodeError:
            return status, body.decode("utf-8", errors="replace")


def check_health(api: Api, report: Report) -> bool:
    ok = True
    for path in ("/health/live", "/health/ready"):
        status, body = api.json("GET", path)
        if status == 200 and isinstance(body, dict) and body.get("status") == "ok":
            detail = "200 ok"
            if isinstance(body.get("jobs"), dict) and body["jobs"].get("worker"):
                detail += f"; worker {body['jobs']['worker']}"
            report.add(f"health {path}", "PASS", detail)
        else:
            report.add(f"health {path}", "FAIL", f"status {status}: {body}")
            ok = False
    return ok


def check_model(api: Api, report: Report) -> None:
    status, body = api.json("GET", "/api/ml/model")
    if status != 200 or not isinstance(body, dict):
        report.add("ml model status", "FAIL", f"status {status}: {body}")
        return
    production = body.get("production") or {}
    report.add("ml model status", "PASS",
               f"ranking_method={body.get('ranking_method')}; usable={production.get('usable')}; "
               f"label_source={production.get('label_source')}; reason={production.get('reason')}")


def choose_candidate(api: Api, report: Report, requested: str | None) -> str | None:
    status, body = api.json("GET", "/api/recommendations?limit=50")
    if status != 200 or not isinstance(body, list) or not body:
        report.add("recommendations available", "FAIL", f"status {status}: {body}")
        return None
    ids = [row.get("candidate_id") for row in body]
    if requested:
        if requested in ids:
            report.add("recommendations available", "PASS", f"{len(ids)} rows, using {requested}")
            return requested
        report.add("recommendations available", "FAIL", f"{requested} not among {ids}")
        return None
    report.add("recommendations available", "PASS", f"{len(ids)} rows, using {ids[0]}")
    return ids[0]


def submit(api: Api, report: Report, candidate: str, iterations: int) -> str | None:
    status, body = api.json(
        "POST", f"/api/benchmark/{urllib.parse.quote(candidate)}?background=true&iterations={iterations}"
    )
    if status == 202 and isinstance(body, dict) and body.get("job_id") and body.get("status") in {"PENDING", "RUNNING"}:
        report.add("submit returns 202 job", "PASS", f"job {body['job_id']} {body['status']} (created={body.get('created')})")
        return body["job_id"]
    report.add("submit returns 202 job", "FAIL", f"status {status}: {body}")
    return None


def poll(api: Api, report: Report, job_id: str, timeout: float, interval: float) -> dict | None:
    deadline = time.monotonic() + timeout
    seen_statuses: list[str] = []
    listed = False
    while time.monotonic() < deadline:
        status, job = api.json("GET", f"/api/jobs/{job_id}")
        if status != 200 or not isinstance(job, dict):
            report.add("job status endpoint", "FAIL", f"status {status}: {job}")
            return None
        state = job.get("status")
        if not seen_statuses or seen_statuses[-1] != state:
            seen_statuses.append(state)
            print(f"    job {job_id} -> {state}", flush=True)
        if not listed:
            _, jobs = api.json("GET", "/api/jobs?kind=benchmark&limit=20")
            if isinstance(jobs, list) and any(j.get("job_id") == job_id for j in jobs):
                listed = True
        if state in TERMINAL:
            report.add("job persisted and listed", "PASS" if listed else "FAIL",
                       "appears in /api/jobs" if listed else "never appeared in /api/jobs")
            error = job.get("error") or {}
            report.add("job reached terminal state", "PASS" if state == "COMPLETED" else "FAIL",
                       f"{' -> '.join(seen_statuses)}" + (f"; error={error.get('code')}: {error.get('message')}" if error else ""))
            return job
        time.sleep(interval)
    report.add("job reached terminal state", "FAIL",
               f"still {seen_statuses[-1] if seen_statuses else 'unknown'} after {timeout:.0f}s. "
               "A job that stays PENDING means no worker shares the API's job store "
               "(see /health/ready jobs.worker).")
    return None


def check_result(api: Api, report: Report, job: dict) -> dict | None:
    result = job.get("result")
    if not isinstance(result, dict):
        report.add("job result", "FAIL", f"no result on completed job: {job}")
        return None
    summary = result.get("summary") or {}
    required = {"before_ms", "after_ms", "speedup", "improvement_percentage", "execution_count", "workload_benefit"}
    missing = sorted(required - summary.keys())
    if missing:
        report.add("result has aggregate fields", "FAIL", f"missing {missing}")
    else:
        report.add("result has aggregate fields", "PASS",
                   f"before {summary['before_ms']} ms, after {summary['after_ms']} ms, "
                   f"{summary['speedup']}x over {summary['execution_count']} executions, "
                   f"workload_benefit {summary['workload_benefit']}, affected {summary.get('affected_queries')}")

    cleanup = result.get("cleanup") or {}
    if cleanup.get("status") == "dropped":
        report.add("cleanup recorded", "PASS", f"worker dropped {cleanup.get('index_name')} after measuring")
    else:
        report.add("cleanup recorded", "FAIL", f"cleanup={cleanup!r}")

    run_id = result.get("run_id")
    status, record = api.json("GET", f"/api/benchmark/history/{run_id}")
    if status == 200 and isinstance(record, dict) and record.get("status") == "COMPLETED":
        rows = record.get("queries") or []
        report.add("run record persisted", "PASS",
                   f"{run_id}: {len(rows)} query groups, protocol v{record.get('protocol_version')}, "
                   f"cleanup {((record.get('cleanup') or {}).get('status'))}")
    else:
        report.add("run record persisted", "FAIL", f"status {status}: {record}")

    _, rows = api.json("GET", "/api/benchmark/results")
    if isinstance(rows, list) and rows and all(row.get("run_id") == run_id for row in rows if row.get("run_id")):
        report.add("measurements in /api/benchmark/results", "PASS", f"{len(rows)} rows for {run_id}")
    else:
        report.add("measurements in /api/benchmark/results", "FAIL", "latest results do not belong to this run")
    return result


def check_artifact(api: Api, report: Report, result: dict, expect_local: bool) -> None:
    if expect_local:
        report.add("artifact reference", "PASS", "local run: history record on the shared volume is the artifact")
        return
    report.add("artifact reference", "PENDING-APP",
               "Per-job S3 artifacts are not implemented on this branch; the run record lives in the task's "
               "benchmark history file. Trained model artifacts are mirrored when ARTIFACT_STORE=s3 "
               "(see /api/ml/model provenance).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QueryScale end-to-end smoke test.")
    parser.add_argument("--base-url", required=True, help="ALB URL, e.g. http://queryscale-dev-api-xxx.elb.amazonaws.com")
    parser.add_argument("--candidate-id", help="Recommendation to benchmark; default is the first listed.")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=1200, help="Seconds to wait for a terminal state.")
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--expect-local-artifacts", action="store_true",
                        help="Local Docker run: accept the local history record and skip S3 checks.")
    parser.add_argument("--tolerate-missing-adapters", action="store_true",
                        help="Exit 0 even when PENDING-APP checks remain (never for FAIL).")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON at the end.")
    args = parser.parse_args(argv)

    api = Api(args.base_url)
    report = Report()
    print(f"Smoke test against {api.base_url}")

    if check_health(api, report):
        check_model(api, report)
        candidate = choose_candidate(api, report, args.candidate_id)
        if candidate:
            job_id = submit(api, report, candidate, args.iterations)
            if job_id:
                job = poll(api, report, job_id, args.timeout, args.poll_interval)
                if job and job.get("status") == "COMPLETED":
                    result = check_result(api, report, job)
                    if result:
                        check_artifact(api, report, result, args.expect_local_artifacts)

    failed, pending = report.failed(), report.pending()
    print(f"\n{len(report.checks) - len(failed) - len(pending)} passed, {len(failed)} failed, {len(pending)} pending application work")
    if args.json:
        print(json.dumps([c.__dict__ for c in report.checks], indent=2))
    if failed:
        return 1
    if pending and not args.tolerate_missing_adapters:
        print("Pending application features block a green result; pass --tolerate-missing-adapters to accept them.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
