<#
.SYNOPSIS
    Run the QueryScale database bootstrap (or validation) as a one-off ECS Fargate task.

.DESCRIPTION
    Reads the Terraform outputs of infra/terraform, starts the bootstrap task definition
    on the ECS cluster with the right subnets and security group, waits for it to stop,
    prints its CloudWatch logs and exits with the container's exit code.

    Requires: aws CLI authenticated to the target account, terraform, and an applied
    main stack with image_tag set (otherwise bootstrap_task_definition_arn is null).

.PARAMETER Mode
    bootstrap (default) runs scripts.aws_bootstrap; validate runs scripts.aws_db_validate.

.PARAMETER ScriptArgs
    Extra arguments passed to the Python module, for example "--rebuild-test-db" or
    "--customers 1000 --transactions 5000".

.EXAMPLE
    .\scripts\aws\run_bootstrap_task.ps1
    .\scripts\aws\run_bootstrap_task.ps1 -Mode validate
    .\scripts\aws\run_bootstrap_task.ps1 -ScriptArgs "--rebuild-test-db"
#>
[CmdletBinding()]
param(
    [ValidateSet("bootstrap", "validate")]
    [string]$Mode = "bootstrap",
    [string[]]$ScriptArgs = @(),
    [string]$TerraformDir = (Join-Path $PSScriptRoot "..\..\infra\terraform"),
    [int]$TimeoutMinutes = 30
)

$ErrorActionPreference = "Stop"

function Get-TfOutput {
    param([string]$Name)
    $raw = terraform -chdir="$TerraformDir" output -json $Name
    if ($LASTEXITCODE -ne 0) { throw "terraform output $Name failed" }
    return $raw | ConvertFrom-Json
}

$cluster  = Get-TfOutput ecs_cluster_name
$taskDef  = Get-TfOutput bootstrap_task_definition_arn
$subnets  = Get-TfOutput ecs_task_subnet_ids
$publicIp = Get-TfOutput ecs_task_assign_public_ip
$sgIds    = Get-TfOutput security_group_ids
$logs     = Get-TfOutput log_group_names
$region   = (terraform -chdir="$TerraformDir" output -json container_environment | ConvertFrom-Json).AWS_REGION

if (-not $taskDef) {
    throw "bootstrap_task_definition_arn is null. Set image_tag in dev.tfvars to a pushed image and apply first."
}

$module = if ($Mode -eq "validate") { "scripts.aws_db_validate" } else { "scripts.aws_bootstrap" }
$command = @("python", "-m", $module) + $ScriptArgs
$overridesJson = @{ containerOverrides = @(@{ name = "bootstrap"; command = $command }) } | ConvertTo-Json -Depth 5 -Compress
# JSON goes through a file: Windows PowerShell 5.1 mangles embedded quotes passed to native commands.
$overridesFile = Join-Path ([System.IO.Path]::GetTempPath()) ("queryscale-overrides-" + [guid]::NewGuid().ToString("N") + ".json")
[System.IO.File]::WriteAllText($overridesFile, $overridesJson)
$overrides = "file://" + ($overridesFile -replace "\\", "/")
$assign = if ($publicIp) { "ENABLED" } else { "DISABLED" }
$network = "awsvpcConfiguration={subnets=[$($subnets -join ',')],securityGroups=[$($sgIds.ecs_tasks)],assignPublicIp=$assign}"

Write-Host "Starting $Mode task on cluster $cluster ($region)..."
$started = aws ecs run-task --region $region --cluster $cluster --launch-type FARGATE `
    --task-definition $taskDef --network-configuration $network --overrides $overrides --output json | ConvertFrom-Json
Remove-Item -ErrorAction SilentlyContinue $overridesFile
if ($LASTEXITCODE -ne 0 -or -not $started.tasks) {
    Write-Error ("run-task failed: " + ($started.failures | ConvertTo-Json -Compress))
    exit 1
}
$taskArn = $started.tasks[0].taskArn
$taskId = $taskArn.Split("/")[-1]
Write-Host "Task $taskId started. Waiting for it to stop (timeout $TimeoutMinutes min)..."

$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
do {
    Start-Sleep -Seconds 15
    $task = (aws ecs describe-tasks --region $region --cluster $cluster --tasks $taskArn --output json | ConvertFrom-Json).tasks[0]
    Write-Host ("  status: " + $task.lastStatus)
} while ($task.lastStatus -ne "STOPPED" -and (Get-Date) -lt $deadline)

if ($task.lastStatus -ne "STOPPED") {
    Write-Error "Task did not stop within $TimeoutMinutes minutes. Inspect it with: aws ecs describe-tasks --cluster $cluster --tasks $taskArn"
    exit 1
}

$exitCode = $task.containers[0].exitCode
Write-Host "Stopped: $($task.stoppedReason)"
Write-Host "---- logs ($($logs.bootstrap)) ----"
aws logs tail $logs.bootstrap --region $region --log-stream-names "bootstrap/bootstrap/$taskId" --since 1h
Write-Host "---- container exit code: $exitCode ----"
if ($null -eq $exitCode) { exit 1 }
exit [int]$exitCode
