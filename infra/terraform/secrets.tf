# Secrets Manager entries for the three application database users. Passwords are
# generated here so no secret value is ever typed into tfvars or committed.
#
# The RDS master credential is NOT defined here: RDS manages it (rds.tf,
# manage_master_user_password) and only the bootstrap execution role may read it.
#
# Each secret stores JSON with username, password, host, port and dbname. ECS injects
# individual keys with the `secrets` block, for example
#   { name = "DB_PASSWORD", valueFrom = "<secret-arn>:password::" }
#
# User            Env (proposed)                  Access
# db_app          DB_USER / DB_PASSWORD           SELECT on DB_NAME
# db_benchmark    DB_TEST_USER / DB_TEST_PASSWORD SELECT, INDEX on DB_TEST_NAME
# db_jobs         DB_JOBS_USER / DB_JOBS_PASSWORD read/write + DDL on DB_JOBS_NAME

locals {
  db_users = {
    app = {
      username    = var.db_app_username
      dbname      = var.db_name
      description = "QueryScale application database user (DB_USER / DB_PASSWORD): SELECT on the source database"
    }
    benchmark = {
      username    = var.db_benchmark_username
      dbname      = var.db_test_name
      description = "QueryScale benchmark database user (DB_TEST_USER / DB_TEST_PASSWORD): SELECT and INDEX on the test database"
    }
    jobs = {
      username    = var.db_jobs_username
      dbname      = var.db_jobs_name
      description = "QueryScale job-metadata database user (DB_JOBS_USER / DB_JOBS_PASSWORD): read/write on the jobs database"
    }
  }
}

resource "random_password" "db_user" {
  for_each = local.db_users

  length           = 32
  special          = true
  override_special = "!#$%^&*()-_=+[]{}<>:?"
}

resource "aws_secretsmanager_secret" "db_user" {
  for_each = local.db_users

  name                    = "${local.name}/db/${each.key}"
  description             = each.value.description
  recovery_window_in_days = var.secret_recovery_window_days

  tags = { Name = "${local.name}-db-${each.key}" }
}

resource "aws_secretsmanager_secret_version" "db_user" {
  for_each = local.db_users

  secret_id = aws_secretsmanager_secret.db_user[each.key].id

  secret_string = jsonencode({
    username = each.value.username
    password = random_password.db_user[each.key].result
    engine   = "mysql"
    host     = aws_db_instance.main.address
    port     = var.db_port
    dbname   = each.value.dbname
  })
}

# Stable aliases used by iam.tf, ecs_bootstrap.tf and outputs.tf.
locals {
  secret_db_app       = aws_secretsmanager_secret.db_user["app"]
  secret_db_benchmark = aws_secretsmanager_secret.db_user["benchmark"]
  secret_db_jobs      = aws_secretsmanager_secret.db_user["jobs"]
}
