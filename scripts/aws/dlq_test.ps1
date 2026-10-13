<#
.SYNOPSIS
    Dead-letter queue validation: a poison message with an unknown job ID ends in the DLQ.

.DESCRIPTION
    Sends a message carrying a job ID that no job store knows, then receives it
    repeatedly with a one-second visibility timeout until SQS exceeds the queue's
    maxReceiveCount and moves it to the dead-letter queue. This exercises the redrive
    policy, the DLQ, and the dlq-has-messages alarm in minutes instead of waiting
    (maxReceiveCount x visibility timeout) for a worker to time out naturally.

    Run with the worker scaled to zero (worker_desired_count = 0) once the SQS adapter
    is deployed, otherwise the worker competes for the message. With the current
    application the worker does not consume from SQS at all, so no scaling is needed.

    Cleans up by deleting the message from the DLQ unless -LeaveMessage is given.

.EXAMPLE
    .\scripts\aws\dlq_test.ps1
#>
[CmdletBinding()]
param(
    [switch]$LeaveMessage,
    [string]$TerraformDir = (Join-Path $PSScriptRoot "..\..\infra\terraform")
)

$ErrorActionPreference = "Stop"

function Get-TfOutput {
    param([string]$Name)
    $raw = terraform -chdir="$TerraformDir" output -json $Name
    if ($LASTEXITCODE -ne 0) { throw "terraform output $Name failed" }
    return $raw | ConvertFrom-Json
}

$queueUrl = Get-TfOutput job_queue_url
$dlqUrl   = Get-TfOutput job_dlq_url
$region   = (Get-TfOutput container_environment).AWS_REGION
$prefix   = ($queueUrl.Split("/")[-1] -replace "-benchmark-jobs\.fifo$", "")

$attrs = (aws sqs get-queue-attributes --region $region --queue-url $queueUrl --attribute-names RedrivePolicy VisibilityTimeout --output json | ConvertFrom-Json).Attributes
$maxReceive = ([int]($attrs.RedrivePolicy | ConvertFrom-Json).maxReceiveCount)
Write-Host "Queue $queueUrl"
Write-Host "  maxReceiveCount=$maxReceive visibilityTimeout=$($attrs.VisibilityTimeout)s"

$unknownJob = "00000000-0000-4000-8000-" + (Get-Date -Format "HHmmssffff").PadLeft(12, "0")
# The body is JSON; pass it through a file because Windows PowerShell 5.1 mangles embedded quotes.
$bodyFile = Join-Path ([System.IO.Path]::GetTempPath()) ("queryscale-dlq-" + [guid]::NewGuid().ToString("N") + ".json")
[System.IO.File]::WriteAllText($bodyFile, (@{ job_id = $unknownJob } | ConvertTo-Json -Compress))
Write-Host "== 1. Send poison message for job $unknownJob"
aws sqs send-message --region $region --queue-url $queueUrl --message-body ("file://" + ($bodyFile -replace "\\", "/")) --message-group-id benchmark --message-deduplication-id $unknownJob --output json | Out-Null
$sendExit = $LASTEXITCODE
Remove-Item -ErrorAction SilentlyContinue $bodyFile
if ($sendExit -ne 0) { throw "send-message failed" }

Write-Host "== 2. Receive it $($maxReceive + 1) times with a 1 s visibility timeout"
$receipts = 0
for ($i = 1; $i -le ($maxReceive + 1); $i++) {
    $msg = aws sqs receive-message --region $region --queue-url $queueUrl --max-number-of-messages 1 --visibility-timeout 1 --wait-time-seconds 5 --attribute-names ApproximateReceiveCount --output json | ConvertFrom-Json
    if ($msg.Messages) {
        $receipts++
        Write-Host ("    receive {0}: ApproximateReceiveCount={1}" -f $i, $msg.Messages[0].Attributes.ApproximateReceiveCount)
        Start-Sleep -Seconds 2
    } else {
        Write-Host "    receive ${i}: no message visible (moved to DLQ or not yet visible)"
    }
}

Write-Host "== 3. Confirm the message is in the dead-letter queue"
$inDlq = $null
for ($i = 0; $i -lt 12 -and -not $inDlq; $i++) {
    $dlq = aws sqs receive-message --region $region --queue-url $dlqUrl --max-number-of-messages 1 --visibility-timeout 30 --wait-time-seconds 5 --output json | ConvertFrom-Json
    if ($dlq.Messages -and $dlq.Messages[0].Body -like "*$unknownJob*") { $inDlq = $dlq.Messages[0] }
}
if (-not $inDlq) {
    Write-Host "[FAIL] poison message did not reach the DLQ after $receipts receives"
    exit 1
}
Write-Host "[PASS] message $unknownJob is in the DLQ"

Write-Host "== 4. Check the dlq-has-messages alarm (metric lag is about one minute)"
$alarmName = "$prefix-dlq-has-messages"
$alarmState = "INSUFFICIENT_DATA"
for ($i = 0; $i -lt 8 -and $alarmState -ne "ALARM"; $i++) {
    Start-Sleep -Seconds 15
    $alarmState = (aws cloudwatch describe-alarms --region $region --alarm-names $alarmName --output json | ConvertFrom-Json).MetricAlarms[0].StateValue
    Write-Host "    $alarmName = $alarmState"
}
if ($alarmState -eq "ALARM") { Write-Host "[PASS] alarm $alarmName is in ALARM" } else { Write-Host "[WARN] alarm $alarmName is $alarmState; check the SNS topic and metric period" }

if (-not $LeaveMessage) {
    aws sqs delete-message --region $region --queue-url $dlqUrl --receipt-handle $inDlq.ReceiptHandle
    Write-Host "== 5. Deleted the poison message from the DLQ; the alarm returns to OK within a few minutes"
}
exit 0
