<#
.SYNOPSIS
    Worker-interruption test: stop the worker task mid-benchmark and verify recovery.

.DESCRIPTION
    1. Submits a benchmark job through the API and waits for it to reach "running".
    2. Stops the running worker ECS task with `aws ecs stop-task` (ECS restarts it).
    3. Polls the job until it reaches a terminal state or the timeout.
    4. Runs the database validation task, whose benchmark:no-leftover-indexes check
       proves the candidate index is absent from the benchmark database.

    Expected outcome once the shared job store and SQS adapters are deployed:
      - the job's attempts counter is at least 2 (the message was redelivered after
        the visibility timeout and the job was claimed again);
      - the job ends succeeded (or failed with worker_interrupted after
        BENCHMARK_JOB_MAX_ATTEMPTS);
      - the validation task reports no leftover index.

    With the current application (task-local SQLite job store) the job disappears with
    the stopped task; the script reports that as PENDING-APP rather than a pass.

.EXAMPLE
    .\scripts\aws\interruption_test.ps1
    .\scripts\aws\interruption_test.ps1 -Iterations 500 -TimeoutMinutes 40
#>
[CmdletBinding()]
param(
    [int]$Iterations = 300,
    [int]$TimeoutMinutes = 45,
    [string]$TerraformDir = (Join-Path $PSScriptRoot "..\..\infra\terraform")
)

$ErrorActionPreference = "Stop"

function Get-TfOutput {
    param([string]$Name)
    $raw = terraform -chdir="$TerraformDir" output -json $Name
    if ($LASTEXITCODE -ne 0) { throw "terraform output $Name failed" }
    return $raw | ConvertFrom-Json
}

$apiUrl   = Get-TfOutput api_url
$cluster  = Get-TfOutput ecs_cluster_name
$services = Get-TfOutput ecs_service_names
$region   = (Get-TfOutput container_environment).AWS_REGION
$results  = @()

function Record { param([string]$Name, [string]$State, [string]$Detail)
    $script:results += [pscustomobject]@{ check = $Name; state = $State; detail = $Detail }
    Write-Host ("[{0,-11}] {1}: {2}" -f $State, $Name, $Detail)
}

Write-Host "== 1. Submit a long benchmark ($Iterations iterations) via $apiUrl"
$candidate = (Invoke-RestMethod "$apiUrl/api/recommendations?limit=1")[0].candidate_id
$job = Invoke-RestMethod -Method Post "$apiUrl/api/benchmark/$candidate`?iterations=$Iterations"
Record "submit" "PASS" "job $($job.job_id) queued for $candidate"

Write-Host "== 2. Wait for the job to be claimed (status running)"
$deadline = (Get-Date).AddMinutes(10)
do {
    Start-Sleep -Seconds 5
    $state = Invoke-RestMethod "$apiUrl/api/benchmark/jobs/$($job.job_id)"
} while ($state.status -eq "queued" -and (Get-Date) -lt $deadline)
if ($state.status -ne "running") {
    Record "job claimed by worker" "PENDING-APP" "status is '$($state.status)' after 10 minutes. The worker cannot see API-submitted jobs until the shared job store and SQS adapters are deployed."
    exit 1
}
Record "job claimed by worker" "PASS" "running, attempts=$($state.attempts)"

Write-Host "== 3. Stop the worker task while the benchmark runs"
$taskArn = (aws ecs list-tasks --region $region --cluster $cluster --service-name $services.worker --desired-status RUNNING --output json | ConvertFrom-Json).taskArns[0]
if (-not $taskArn) { throw "No running worker task found in service $($services.worker)" }
aws ecs stop-task --region $region --cluster $cluster --task $taskArn --reason "interruption test" --output json | Out-Null
Record "worker task stopped" "PASS" $taskArn.Split("/")[-1]

Write-Host "== 4. Poll the job to a terminal state (ECS restarts the worker; SQS redelivers after the visibility timeout)"
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds 15
    try { $state = Invoke-RestMethod "$apiUrl/api/benchmark/jobs/$($job.job_id)" } catch { $state = $null }
    $status = if ($state) { $state.status } else { "unknown" }
    Write-Host "    $status attempts=$($state.attempts)"
} while ($status -in @("queued", "running") -and (Get-Date) -lt $deadline)

if ($status -eq "unknown") {
    Record "job survives worker restart" "PENDING-APP" "job no longer visible: it lived in the stopped task's local SQLite store (shared job-store adapter not deployed)"
} elseif ($state.attempts -ge 2 -and $status -in @("succeeded", "failed")) {
    Record "job redelivered and recovered" "PASS" "status=$status attempts=$($state.attempts) error=$($state.error_code)"
} elseif ($status -in @("succeeded", "failed")) {
    Record "job redelivered and recovered" "FAIL" "terminal ($status) but attempts=$($state.attempts); expected at least 2"
} else {
    Record "job redelivered and recovered" "FAIL" "still $status after $TimeoutMinutes minutes"
}

Write-Host "== 5. Verify the candidate index is absent from the benchmark database"
& (Join-Path $PSScriptRoot "run_bootstrap_task.ps1") -Mode validate
if ($LASTEXITCODE -eq 0) { Record "no leftover index (validation task)" "PASS" "benchmark:no-leftover-indexes passed" }
else { Record "no leftover index (validation task)" "FAIL" "validation task exit $LASTEXITCODE; see its log output above" }

Write-Host ""
$results | Format-Table -AutoSize
if ($results.state -contains "FAIL") { exit 1 }
if ($results.state -contains "PENDING-APP") { exit 1 }
exit 0
