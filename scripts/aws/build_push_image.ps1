<#
.SYNOPSIS
    Build the QueryScale image and push it to ECR tagged with the current Git commit.

.DESCRIPTION
    Reads the ECR repository URL from the Terraform outputs, logs Docker in to ECR, builds
    the repository Dockerfile for the configured CPU architecture and pushes it as
    <repo>:<short-sha>. The repository rejects tag overwrites, so a rebuild of the same
    commit must use -Force to add a "-<n>" suffix instead of failing.

    After pushing, set image_tag in infra/terraform/environments/dev.tfvars to the
    printed tag and run terraform apply to roll the API and worker services.

.PARAMETER Platform
    Docker platform to build for. Must match image_cpu_architecture in dev.tfvars
    (linux/amd64 -> X86_64, linux/arm64 -> ARM64).

.PARAMETER AllowDirty
    Build even when the working tree has uncommitted changes. The tag still names the
    last commit, so the image would not match it; off by default.

.EXAMPLE
    .\scripts\aws\build_push_image.ps1
    .\scripts\aws\build_push_image.ps1 -Platform linux/arm64
#>
[CmdletBinding()]
param(
    [ValidateSet("linux/amd64", "linux/arm64")]
    [string]$Platform = "linux/amd64",
    [switch]$AllowDirty,
    [switch]$Force,
    [string]$TerraformDir = (Join-Path $PSScriptRoot "..\..\infra\terraform")
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")

$dirty = git -C $repoRoot status --porcelain
if ($dirty -and -not $AllowDirty) {
    throw "Working tree has uncommitted changes. Commit them or pass -AllowDirty (the tag names the last commit only)."
}
$sha = (git -C $repoRoot rev-parse --short=12 HEAD).Trim()
if (-not $sha) { throw "Unable to read the current Git commit." }

$repoUrl = (terraform -chdir="$TerraformDir" output -raw ecr_repository_url).Trim()
if ($LASTEXITCODE -ne 0 -or -not $repoUrl) { throw "terraform output ecr_repository_url failed. Apply the main stack first." }
$region = (terraform -chdir="$TerraformDir" output -json container_environment | ConvertFrom-Json).AWS_REGION
$registry = $repoUrl.Split("/")[0]

$tag = $sha
if ($Force) {
    $existing = aws ecr list-images --region $region --repository-name $repoUrl.Split("/")[-1] --query "imageIds[?starts_with(imageTag, '$sha')].imageTag" --output json | ConvertFrom-Json
    if ($existing) { $tag = "$sha-$($existing.Count)" }
}

Write-Host "Logging in to $registry ($region)..."
aws ecr get-login-password --region $region | docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { throw "ECR login failed" }

$image = "${repoUrl}:${tag}"
Write-Host "Building $image for $Platform..."
docker build --platform $Platform -t $image $repoRoot
if ($LASTEXITCODE -ne 0) { throw "docker build failed" }

Write-Host "Pushing $image..."
docker push $image
if ($LASTEXITCODE -ne 0) { throw "docker push failed (an existing tag cannot be overwritten; use -Force to add a suffix)" }

Write-Host ""
Write-Host "Pushed $image"
Write-Host "Next: set image_tag = `"$tag`" in infra/terraform/environments/dev.tfvars, then:"
Write-Host "  terraform -chdir=infra/terraform plan -var-file=environments/dev.tfvars -out=dev.tfplan"
Write-Host "  terraform -chdir=infra/terraform apply dev.tfplan"
