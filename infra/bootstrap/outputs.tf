output "state_bucket_name" {
  description = "Bucket name to place in infra/terraform/backend.hcl."
  value       = aws_s3_bucket.state.bucket
}

output "state_bucket_region" {
  description = "Region to place in infra/terraform/backend.hcl."
  value       = var.region
}
