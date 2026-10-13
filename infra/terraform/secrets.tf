# Secrets Manager containers for the two application database users named in
# .env.example (DB_USER / DB_TEST_USER). Passwords are generated here so no secret
# value is ever typed into tfvars or committed. The RDS master credential is NOT
# created here: the RDS stage will let RDS manage it and only the bootstrap task
# will be allowed to read it.
#
# Each secret stores JSON with username, password and dbname. The RDS stage adds host
# and port by writing a new version, which is why secret_string changes are ignored
# after creation. ECS injects individual keys with the `secrets` block:
#   { name = "DB_PASSWORD", valueFrom = "<secret-arn>:password::" }

resource "random_password" "db_app" {
  length           = 32
  special          = true
  override_special = "!#$%^&*()-_=+[]{}<>:?"
}

resource "random_password" "db_benchmark" {
  length           = 32
  special          = true
  override_special = "!#$%^&*()-_=+[]{}<>:?"
}

resource "aws_secretsmanager_secret" "db_app" {
  name                    = "${local.name}/db/app"
  description             = "QueryScale application database user (DB_USER / DB_PASSWORD)"
  recovery_window_in_days = var.secret_recovery_window_days

  tags = { Name = "${local.name}-db-app" }
}

resource "aws_secretsmanager_secret_version" "db_app" {
  secret_id = aws_secretsmanager_secret.db_app.id

  secret_string = jsonencode({
    username = var.db_app_username
    password = random_password.db_app.result
    dbname   = var.db_name
    jobs_db  = var.db_jobs_name
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "db_benchmark" {
  name                    = "${local.name}/db/benchmark"
  description             = "QueryScale benchmark database user (DB_TEST_USER / DB_TEST_PASSWORD)"
  recovery_window_in_days = var.secret_recovery_window_days

  tags = { Name = "${local.name}-db-benchmark" }
}

resource "aws_secretsmanager_secret_version" "db_benchmark" {
  secret_id = aws_secretsmanager_secret.db_benchmark.id

  secret_string = jsonencode({
    username = var.db_benchmark_username
    password = random_password.db_benchmark.result
    dbname   = var.db_test_name
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
