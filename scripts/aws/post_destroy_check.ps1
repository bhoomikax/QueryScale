<#
.SYNOPSIS
    After terraform destroy, look for QueryScale resources that still exist and may bill.

.DESCRIPTION
    Read-only. Sweeps the account in the given region for resources named or tagged for
    this environment that Terraform does not remove or that commonly linger: RDS instances
    and snapshots (manual and final), network interfaces, VPCs, NAT gateways, elastic IPs,
    load balancers and target groups, ECS clusters, ECR repositories, S3 buckets, SQS
    queues, Secrets Manager secrets (including ones scheduled for deletion), SNS topics,
    CloudWatch log groups and alarms, budgets, and anything else carrying the stack tags.

    The Terraform state bucket from infra/bootstrap is reported but expected; delete it
    by hand only when the environment is retired for good.

    Exit code 1 when anything other than the state bucket is found.

.EXAMPLE
    .\scripts\aws\post_destroy_check.ps1 -Environment dev
    .\scripts\aws\post_destroy_check.ps1 -Environment dev -Region eu-west-1 -NamePrefix queryscale
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Environment,
    [string]$NamePrefix = "queryscale",
    [string]$Region = "us-east-1"
)

$ErrorActionPreference = "Continue"
$name = "$NamePrefix-$Environment"
$found = New-Object System.Collections.Generic.List[string]

function Report {
    param([string]$Kind, $Items, [switch]$Expected)
    $list = @($Items | Where-Object { $_ })
    if ($list.Count -eq 0) { Write-Host ("  ok    {0}" -f $Kind); return }
    foreach ($item in $list) {
        $flag = if ($Expected) { "note " } else { "FOUND" }
        Write-Host ("  {0} {1}: {2}" -f $flag, $Kind, $item)
        if (-not $Expected) { $script:found.Add("$Kind $item") }
    }
}

function Aws {
    param([string[]]$CommandArgs)
    $out = & aws @CommandArgs --region $Region --output json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) { return @() }
    return ($out | ConvertFrom-Json)
}

$account = (Aws @("sts", "get-caller-identity")).Account
Write-Host "Post-destroy check for '$name' in $Region (account $account)"
Write-Host ""

Write-Host "== Tagged resources (Project=$NamePrefix, Environment=$Environment)"
$tagged = Aws @("resourcegroupstaggingapi", "get-resources", "--tag-filters", "Key=Project,Values=$NamePrefix", "Key=Environment,Values=$Environment")
$stateBucketArns = @($tagged.ResourceTagMappingList | Where-Object { $_.ResourceARN -like "arn:aws:s3:::$name-tfstate-*" } | ForEach-Object { $_.ResourceARN })
$otherTagged = @($tagged.ResourceTagMappingList | Where-Object { $_.ResourceARN -notin $stateBucketArns } | ForEach-Object { $_.ResourceARN })
Report "state bucket (expected, delete by hand when retiring)" $stateBucketArns -Expected
Report "tagged resource" $otherTagged

Write-Host "== RDS"
Report "db instance" (Aws @("rds", "describe-db-instances", "--query", "DBInstances[?starts_with(DBInstanceIdentifier, '$name')].DBInstanceIdentifier"))
Report "db snapshot (manual or final; billed for storage)" (Aws @("rds", "describe-db-snapshots", "--query", "DBSnapshots[?starts_with(DBSnapshotIdentifier, '$name') || starts_with(DBInstanceIdentifier, '$name')].DBSnapshotIdentifier"))
Report "db subnet group" (Aws @("rds", "describe-db-subnet-groups", "--query", "DBSubnetGroups[?starts_with(DBSubnetGroupName, '$name')].DBSubnetGroupName"))
Report "db parameter group" (Aws @("rds", "describe-db-parameter-groups", "--query", "DBParameterGroups[?starts_with(DBParameterGroupName, '$name')].DBParameterGroupName"))

Write-Host "== Network"
Report "vpc" (Aws @("ec2", "describe-vpcs", "--filters", "Name=tag:Name,Values=$name-vpc", "--query", "Vpcs[].VpcId"))
Report "network interface" (Aws @("ec2", "describe-network-interfaces", "--filters", "Name=tag:Project,Values=$NamePrefix", "--query", "NetworkInterfaces[].NetworkInterfaceId"))
Report "nat gateway (not deleted)" (Aws @("ec2", "describe-nat-gateways", "--filter", "Name=tag:Name,Values=$name-nat", "--query", "NatGateways[?State!='deleted'].NatGatewayId"))
Report "elastic ip" (Aws @("ec2", "describe-addresses", "--filters", "Name=tag:Name,Values=$name-nat-eip", "--query", "Addresses[].AllocationId"))
Report "security group" (Aws @("ec2", "describe-security-groups", "--filters", "Name=group-name,Values=$name-*", "--query", "SecurityGroups[].GroupId"))
Report "vpc endpoint" (Aws @("ec2", "describe-vpc-endpoints", "--filters", "Name=tag:Name,Values=$name-s3-endpoint", "--query", "VpcEndpoints[?State!='deleted'].VpcEndpointId"))

Write-Host "== Load balancing and compute"
Report "load balancer" (Aws @("elbv2", "describe-load-balancers", "--query", "LoadBalancers[?starts_with(LoadBalancerName, '$name')].LoadBalancerArn"))
Report "target group" (Aws @("elbv2", "describe-target-groups", "--query", "TargetGroups[?starts_with(TargetGroupName, 'qsapi-')].TargetGroupArn"))
Report "ecs cluster" (Aws @("ecs", "describe-clusters", "--clusters", $name, "--query", "clusters[?status!='INACTIVE'].clusterArn"))
Report "ecs task definition family (free, but lists forever)" (Aws @("ecs", "list-task-definition-families", "--family-prefix", $name, "--status", "ACTIVE", "--query", "families"))
Report "ecr repository" (Aws @("ecr", "describe-repositories", "--query", "repositories[?repositoryName=='$name'].repositoryName"))

Write-Host "== Storage, queues, secrets, messaging"
Report "s3 bucket" (Aws @("s3api", "list-buckets", "--query", "Buckets[?starts_with(Name, '$name-artifacts')].Name"))
Report "sqs queue" (Aws @("sqs", "list-queues", "--queue-name-prefix", $name, "--query", "QueueUrls"))
Report "secret (including scheduled for deletion)" (Aws @("secretsmanager", "list-secrets", "--include-planned-deletion", "--query", "SecretList[?starts_with(Name, '$name/') || starts_with(Name, 'rds!db-')].Name"))
Report "sns topic" (Aws @("sns", "list-topics", "--query", "Topics[?contains(TopicArn, ':$name-')].TopicArn"))

Write-Host "== Monitoring and billing"
Report "log group" (Aws @("logs", "describe-log-groups", "--log-group-name-prefix", "/$NamePrefix/$Environment", "--query", "logGroups[].logGroupName"))
Report "rds log group" (Aws @("logs", "describe-log-groups", "--log-group-name-prefix", "/aws/rds/instance/$name", "--query", "logGroups[].logGroupName"))
Report "ecs insights log group" (Aws @("logs", "describe-log-groups", "--log-group-name-prefix", "/aws/ecs/containerinsights/$name", "--query", "logGroups[].logGroupName"))
Report "cloudwatch alarm" (Aws @("cloudwatch", "describe-alarms", "--alarm-name-prefix", $name, "--query", "MetricAlarms[].AlarmName"))
Report "budget" (Aws @("budgets", "describe-budgets", "--account-id", $account, "--query", "Budgets[?starts_with(BudgetName, '$name')].BudgetName"))

Write-Host ""
if ($found.Count -eq 0) {
    Write-Host "Nothing left except the state bucket. Delete it by hand only when retiring the environment."
    exit 0
}
Write-Host "$($found.Count) leftover item(s) found. Secrets scheduled for deletion disappear on their own; snapshots, ENIs, NAT gateways, EIPs and load balancers must be removed to stop billing."
exit 1
