terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Partial backend configuration. Bucket, key and region come from backend.hcl
  # (gitignored; see backend.hcl.example) via `terraform init -backend-config=backend.hcl`.
  # `use_lockfile` enables native S3 state locking (Terraform >= 1.10), so no DynamoDB table.
  backend "s3" {
    use_lockfile = true
    encrypt      = true
  }
}
