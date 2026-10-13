# QueryScale main infrastructure stack

See the "Deploy to AWS" section of the repository README for the full workflow.

Files:

| File | Purpose |
| --- | --- |
| `versions.tf` | Terraform and provider pins, partial S3 backend |
| `providers.tf` | AWS provider with default tags |
| `variables.tf` | All configurable inputs with validation |
| `locals.tf`, `data.tf` | Derived names and account/region lookups |
| `network.tf` | VPC, public/private subnets, routes, optional NAT, S3 gateway endpoint |
| `security_groups.tf` | ALB -> ECS tasks -> RDS chain |
| `ecr.tf` | Immutable, scanned image repository with lifecycle policy |
| `s3.tf` | Encrypted private artifact bucket with expiration |
| `sqs.tf` | FIFO job queue and dead-letter queue |
| `secrets.tf` | Secrets Manager containers with generated passwords |
| `cloudwatch.tf` | Log groups with retention |
| `iam.tf` | Execution roles and least-privilege task roles |
| `budget.tf` | Monthly budget with email alerts |
| `outputs.tf` | Values consumed by later stages and by the application |

Later stages (not yet implemented): RDS, ECS cluster/services, ALB, CloudWatch alarms.
