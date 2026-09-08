resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.name}/${var.environment}/database-url"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = local.database_url
}

resource "aws_secretsmanager_secret" "webhook_secrets" {
  name                    = "${var.name}/${var.environment}/webhook-secrets"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "webhook_secrets" {
  secret_id     = aws_secretsmanager_secret.webhook_secrets.id
  secret_string = jsonencode(var.webhook_secrets)
}

resource "aws_secretsmanager_secret" "admin_api_keys" {
  name                    = "${var.name}/${var.environment}/admin-api-keys"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "admin_api_keys" {
  secret_id     = aws_secretsmanager_secret.admin_api_keys.id
  secret_string = jsonencode(var.admin_api_keys)
}

locals {
  destination_names = toset(nonsensitive(keys(var.destination_secrets)))
}

resource "aws_secretsmanager_secret" "destination_secrets" {
  for_each                = local.destination_names
  name                    = "${var.name}/${var.environment}/destination/${each.key}"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "destination_secrets" {
  for_each      = local.destination_names
  secret_id     = aws_secretsmanager_secret.destination_secrets[each.key].id
  secret_string = var.destination_secrets[each.key]
}
