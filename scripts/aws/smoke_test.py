#!/usr/bin/env python3
"""End-to-end smoke test for a deployed (or local) QueryScale API.

Submits one benchmark job through the public API, polls it to a terminal state, fetches
the structured result and the report artifact, and checks the cleanup guarantee. Works
against the ALB URL in AWS or http://127.0.0.1:8000 locally; only the standard library
is used so it runs anywhere Python 3.11+ is installed.

Each check ends in one of three states:

  PASS         behaviour verified
  FAIL         behaviour wrong; exit code 1
  PENDING-APP  depends on an application feature that is not implemented yet
               (cloud artifact references, the artifact download endpoint, the
               cleanup_verified result field). Exit code 1 unless
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
            report.add(f"health {path}", "PASS", "200 ok")
        else:
            report.add(f"health {path}", "FAIL", f"status {status}: {body}")
            ok = False
    return ok


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
    status, body = api.json("POST", f"/api/benchmark/{urllib.parse.quote(candidate)}?iterations={iterations}")
    if status == 202 and isinstance(body, dict) and body.get("status") == "queued" and body.get("job_id"):
        report.add("submit returns 202 queued", "PASS", f"job {body['job_id']}")
        return body["job_id"]
    report.add("submit returns 202 queued", "FAIL", f"status {status}: {body}")
    return None


def poll(api: Api, report: Report, job_id: str, timeout: float, interval: float) -> dict | None:
    deadline = time.monotonic() + timeout
    seen_statuses: list[str] = []
    listed = False
    while time.monotonic() < deadline:
        status, job = api.json("GET", f"/api/benchmark/jobs/{job_id}")
        if status != 200 or not isinstance(job, dict):
            report.add("job status endpoint", "FAIL", f"status {status}: {job}")
            return None
        state = job.get("status")
        if not seen_statuses or seen_statuses[-1] != state:
            seen_statuses.append(state)
            print(f"    job {job_id} -> {state} (attempts={job.get('attempts')})", flush=True)
        if not listed:
            _, jobs = api.json("GET", "/api/benchmark/jobs?limit=20")
            if isinstance(jobs, list) and any(j.get("job_id") == job_id for j in jobs):
                listed = True
        if state in ("succeeded", "failed"):
            report.add("job persisted and listed", "PASS" if listed else "FAIL",
                       "appears in /api/benchmark/jobs" if listed else "never appeared in /api/benchmark/jobs")
            report.add("job reached terminal state", "PASS" if state == "succeeded" else "FAIL",
                       f"{' -> '.join(seen_statuses)}; error={job.get('error_code')}: {job.get('error')}")
            return job
        time.sleep(interval)
    report.add("job reached terminal state", "FAIL",
               f"still {seen_statuses[-1] if seen_statuses else 'unknown'} after {timeout:.0f}s. "
               "If the job stays queued in AWS, the worker cannot see the API task's job store "
               "(shared job-store adapter not deployed).")
    return None


def check_result(api: Api, report: Report, job: dict, require_cleanup_verified: bool) -> None:
    status, result = api.json("GET", f"/api/benchmark/jobs/{job['job_id']}/result")
    if status != 200 or not isinstance(result, dict):
        report.add("result endpoint", "FAIL", f"status {status}: {result}")
        return
    required = {"before_ms", "after_ms", "speedup", "improvement_percentage", "execution_count", "results"}
    missing = sorted(required - result.keys())
    if missing:
        report.add("result has aggregate fields", "FAIL", f"missing {missing}")
    else:
        report.add("result has aggregate fields", "PASS",
                   f"before {result['before_ms']} ms, after {result['after_ms']} ms, "
                   f"{result['speedup']}x over {result['execution_count']} executions, "
                   f"{len(result['results'])} query groups")

    if "cleanup_verified" in result:
        if result["cleanup_verified"] is True:
            report.add("cleanup_verified", "PASS", "worker confirmed the candidate index is absent after cleanup")
        else:
            report.add("cleanup_verified", "FAIL", f"cleanup_verified={result['cleanup_verified']!r}")
    else:
        state = "PENDING-APP" if not require_cleanup_verified else "FAIL"
        report.add("cleanup_verified", state,
                   "result has no cleanup_verified field. Application dependency: the worker must "
                   "re-check candidate_index_exists after remove_candidate_index and record the outcome "
                   "in the result (backend/jobs/worker.py). Until then verify index absence with "
                   "scripts/aws_db_validate.py (benchmark:no-leftover-indexes).")

    _, rows = api.json("GET", "/api/benchmark/results")
    if isinstance(rows, list) and any(row.get("job_id") == job["job_id"] for row in rows):
        report.add("measurements merged into /api/benchmark/results", "PASS", f"{len(rows)} rows total")
    else:
        report.add("measurements merged into /api/benchmark/results", "FAIL", "job rows not present")


def check_artifact(api: Api, report: Report, job: dict, expect_local: bool) -> None:
    ref = job.get("artifact_ref") or ""
    if expect_local:
        if ref.startswith("local-job-artifact:"):
            report.add("artifact reference", "PASS", ref)
        else:
            report.add("artifact reference", "FAIL", f"expected a local reference, got {ref!r}")
        return

    if ref.startswith("s3://"):
        report.add("artifact reference", "PASS", ref)
    elif ref.startswith("local-job-artifact:"):
        report.add("artifact reference", "PENDING-APP",
                   f"{ref}: the worker still writes to task-local disk. Application dependency: "
                   "S3 artifact adapter writing <JOB_ARTIFACT_PREFIX>/<job_id>/results.csv to JOB_ARTIFACT_BUCKET.")
        return
    else:
        report.add("artifact reference", "FAIL", f"unexpected reference {ref!r}")
        return

    status, body = api.json("GET", f"/api/benchmark/jobs/{job['job_id']}/artifact")
    if status == 404:
        report.add("artifact download", "PENDING-APP",
                   "GET /api/benchmark/jobs/{id}/artifact is not implemented. Application dependency: "
                   "return {\"url\": <presigned S3 GET URL>} using the API task role.")
        return
    url = body.get("url") if isinstance(body, dict) else None
    if status != 200 or not url:
        report.add("artifact download", "FAIL", f"status {status}: {body}")
        return
    dl_status, content, _ = api.request("GET", url, absolute=True)
    first_line = content.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    if dl_status == 200 and first_line.startswith("candidate_id,query_id,"):
        report.add("artifact download", "PASS", f"{len(content)} bytes via presigned URL, CSV header ok")
    else:
        report.add("artifact download", "FAIL", f"status {dl_status}, first line {first_line!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QueryScale end-to-end smoke test.")
    parser.add_argument("--base-url", required=True, help="ALB URL, e.g. http://queryscale-dev-api-xxx.elb.amazonaws.com")
    parser.add_argument("--candidate-id", help="Recommendation to benchmark; default is the first listed.")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=1200, help="Seconds to wait for a terminal state.")
    parser.add_argument("--poll-interval", type=float, default=5)
    parser.add_argument("--expect-local-artifacts", action="store_true",
                        help="Local Docker run: accept local-job-artifact references and skip S3 checks.")
    parser.add_argument("--require-cleanup-verified", action="store_true",
                        help="Treat a missing cleanup_verified field as FAIL instead of PENDING-APP.")
    parser.add_argument("--tolerate-missing-adapters", action="store_true",
                        help="Exit 0 even when PENDING-APP checks remain (never for FAIL).")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON at the end.")
    args = parser.parse_args(argv)

    api = Api(args.base_url)
    report = Report()
    print(f"Smoke test against {api.base_url}")

    if check_health(api, report):
        candidate = choose_candidate(api, report, args.candidate_id)
        if candidate:
            job_id = submit(api, report, candidate, args.iterations)
            if job_id:
                job = poll(api, report, job_id, args.timeout, args.poll_interval)
                if job and job.get("status") == "succeeded":
                    check_result(api, report, job, args.require_cleanup_verified)
                    check_artifact(api, report, job, args.expect_local_artifacts)

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
