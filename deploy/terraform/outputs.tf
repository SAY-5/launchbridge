output "base_url" {
  description = "Public base URL of the API (pass as BASE_URL to make smoke)"
  value       = "http://${aws_lb.this.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.this.repository_url
}

output "ecs_cluster" {
  value = aws_ecs_cluster.this.name
}

output "migrate_task_definition" {
  description = "Run once per release: aws ecs run-task --task-definition <this>"
  value       = aws_ecs_task_definition.migrate.arn
}

output "private_subnets" {
  value = aws_subnet.private[*].id
}

output "app_security_group" {
  value = aws_security_group.app.id
}

output "database_endpoint" {
  value = aws_db_instance.this.address
}
