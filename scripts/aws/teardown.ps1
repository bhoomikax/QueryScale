<#
.SYNOPSIS
    Tear down the QueryScale main stack while preserving the Terraform state backend.

.DESCRIPTION
    Two Terraform runs, both shown as plans first:

      1. apply   with the teardown flags turned on (RDS deletion protection off, bucket
                 and ECR force-delete, zero-day secret recovery, ALB deletion protection
                 off). AWS enforces RDS deletion protection at the API, so this step must
                 complete before a destroy can succeed.
      2. destroy with the same flags.

    The bootstrap stack (infra/bootstrap, the S3 state bucket) is NOT touched. Remove it
    by hand, last, after scripts/aws/post_destroy_check.ps1 reports nothing left.

    By default a final RDS snapshot is kept, which continues to bill for its storage;
    pass -SkipFinalSnapshot to drop the data entirely. Automated RDS backups are always
    removed with the instance.

.PARAMETER Environment
    Must equal the environment in the tfvars file. Typed confirmation guards against
    pointing the script at the wrong account.

.PARAMETER PlanOnly
    Show both plans and stop without applying or destroying anything.

.EXAMPLE
    .\scripts\aws\teardown.ps1 -Environment dev -PlanOnly
    .\scripts\aws\teardown.ps1 -Environment dev
    .\scripts\aws\teardown.ps1 -Environment dev -SkipFinalSnapshot
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Environment,
    [string]$VarFile = "environments/dev.tfvars",
    [switch]$SkipFinalSnapshot,
    [switch]$PlanOnly,
    [string]$TerraformDir = (Join-Path $PSScriptRoot "..\..\infra\terraform")
)

$ErrorActionPreference = "Stop"

$teardownVars = @(
    "-var", "db_deletion_protection=false",
    "-var", "db_skip_final_snapshot=$($SkipFinalSnapshot.IsPresent.ToString().ToLower())",
    "-var", "artifact_bucket_force_destroy=true",
    "-var", "ecr_force_delete=true",
    "-var", "secret_recovery_window_days=0",
    "-var", "alb_deletion_protection=false",
    "-var", "api_desired_count=0",
    "-var", "worker_desired_count=0"
)

Write-Host "Terraform directory : $TerraformDir"
Write-Host "Variable file       : $VarFile"
Write-Host "Final RDS snapshot  : $(if ($SkipFinalSnapshot) { 'SKIPPED (data is lost)' } else { 'kept (billed until deleted)' })"
Write-Host ""

$workspaceEnv = (terraform -chdir="$TerraformDir" output -json database_names 2>$null | ConvertFrom-Json)
if (-not $workspaceEnv) { throw "No applied state found in $TerraformDir (terraform output failed). Nothing to tear down." }
$configuredEnv = (Get-Content (Join-Path $TerraformDir $VarFile) | Select-String -Pattern '^\s*environment\s*=\s*"([^"]+)"').Matches.Groups[1].Value
if ($configuredEnv -and $configuredEnv -ne $Environment) {
    throw "The var file sets environment = '$configuredEnv' but you passed -Environment '$Environment'."
}

Write-Host "== Step 1/2: plan the teardown flags"
terraform -chdir="$TerraformDir" plan -var-file="$VarFile" @teardownVars -out=teardown-flags.tfplan
if ($LASTEXITCODE -ne 0) { throw "plan failed" }

Write-Host "== Step 2/2: plan the destroy"
terraform -chdir="$TerraformDir" plan -destroy -var-file="$VarFile" @teardownVars
if ($LASTEXITCODE -ne 0) { throw "destroy plan failed" }

if ($PlanOnly) {
    Write-Host "`nPlan-only run complete. Nothing was changed."
    exit 0
}

Write-Host ""
Write-Host "This will DESTROY every resource in the '$Environment' main stack, including the RDS instance."
$typed = Read-Host "Type the environment name '$Environment' to continue"
if ($typed -ne $Environment) { throw "Confirmation did not match. Aborted." }

Write-Host "`n== Applying teardown flags (removes RDS deletion protection, scales services to 0)"
terraform -chdir="$TerraformDir" apply teardown-flags.tfplan
if ($LASTEXITCODE -ne 0) { throw "apply of teardown flags failed; the stack is unchanged apart from what the plan showed" }

# Destroy re-plans against the state the flag apply just produced.
Write-Host "`n== Destroying"
terraform -chdir="$TerraformDir" destroy -var-file="$VarFile" @teardownVars -auto-approve
if ($LASTEXITCODE -ne 0) {
    Write-Error "destroy did not complete. Re-run this script; Terraform resumes from state. Common blockers: an unconfirmed SNS email subscription (delete it in the console) or an RDS snapshot in progress."
    exit 1
}

Remove-Item -ErrorAction SilentlyContinue (Join-Path $TerraformDir "teardown-flags.tfplan")

Write-Host ""
Write-Host "Main stack destroyed. Next:"
Write-Host "  1. .\scripts\aws\post_destroy_check.ps1 -Environment $Environment   # look for anything still billing"
Write-Host "  2. If a final snapshot was kept and is no longer needed:"
Write-Host "       aws rds describe-db-snapshots --query `"DBSnapshots[?starts_with(DBSnapshotIdentifier, 'queryscale-$Environment')].DBSnapshotIdentifier`""
Write-Host "       aws rds delete-db-snapshot --db-snapshot-identifier <id>"
Write-Host "  3. Only when retiring the environment for good, remove the state bucket by hand:"
Write-Host "       aws s3 rm s3://<state bucket> --recursive ; aws s3api delete-bucket --bucket <state bucket>"
Write-Host "     (infra/bootstrap sets prevent_destroy, so terraform destroy refuses on purpose)."
