<#
.SYNOPSIS
    Check the deployed QueryScale API through the load balancer and report ECS service state.

.DESCRIPTION
    Calls /health/live, /health/ready, /api/recommendations and /dashboard/ on the ALB,
    then prints the deployment status, running counts and most recent events of the API
    and worker services. Exit code 1 if any HTTP check fails or a service is not steady.
    Must be run from an address included in alb_allowed_cidrs.

.EXAMPLE
    .\scripts\aws\check_health.ps1
#>
[CmdletBinding()]
param(
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
$failed   = 0

Write-Host "== HTTP checks against $apiUrl"
foreach ($path in @("/health/live", "/health/ready", "/api/recommendations?limit=1", "/dashboard/")) {
    try {
        $response = Invoke-WebRequest -Uri "$apiUrl$path" -UseBasicParsing -TimeoutSec 15
        Write-Host ("  [{0}] {1}" -f $response.StatusCode, $path)
    } catch {
        $failed++
        Write-Host ("  [FAIL] {0}: {1}" -f $path, $_.Exception.Message)
    }
}

Write-Host "== ECS services on $cluster"
foreach ($name in @($services.api, $services.worker)) {
    if (-not $name) { continue }
    $svc = (aws ecs describe-services --region $region --cluster $cluster --services $name --output json | ConvertFrom-Json).services[0]
    $deployment = $svc.deployments | Where-Object { $_.status -eq "PRIMARY" } | Select-Object -First 1
    $steady = ($svc.runningCount -eq $svc.desiredCount) -and ($deployment.rolloutState -eq "COMPLETED")
    if (-not $steady -and $svc.desiredCount -gt 0) { $failed++ }
    Write-Host ("  {0}: desired={1} running={2} pending={3} rollout={4} taskdef={5}" -f `
        $name, $svc.desiredCount, $svc.runningCount, $svc.pendingCount, $deployment.rolloutState, ($deployment.taskDefinition.Split("/")[-1]))
    $svc.events | Select-Object -First 3 | ForEach-Object { Write-Host ("    {0} {1}" -f $_.createdAt, $_.message) }
}

if ($failed -gt 0) {
    Write-Host "`n$failed check(s) failed. See the troubleshooting section of the README."
    exit 1
}
Write-Host "`nAll checks passed."
exit 0
