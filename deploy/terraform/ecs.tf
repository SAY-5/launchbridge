resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${var.name}-${var.environment}"
  retention_in_days = var.log_retention_days
}

resource "aws_ecs_cluster" "this" {
  name = "${var.name}-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_service_discovery_private_dns_namespace" "this" {
  name = "${var.name}.local"
  vpc  = aws_vpc.this.id
}

resource "aws_service_discovery_service" "receiver" {
  count = var.deploy_receiver_fake ? 1 : 0
  name  = "receiver"

  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.this.id
    dns_records {
      ttl  = 10
      type = "A"
    }
  }
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-${var.environment}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = concat(
      [
        aws_secretsmanager_secret.database_url.arn,
        aws_secretsmanager_secret.webhook_secrets.arn,
        aws_secretsmanager_secret.admin_api_keys.arn,
      ],
      [for s in aws_secretsmanager_secret.destination_secrets : s.arn],
    )
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-${var.environment}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

locals {
  image        = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
  receiver_url = var.deploy_receiver_fake ? "http://receiver.${aws_service_discovery_private_dns_namespace.this.name}:8081" : "http://receiver.invalid"

  common_environment = [
    { name = "LAUNCHBRIDGE_DESTINATIONS_FILE", value = "/app/destinations.yaml" },
    { name = "LAUNCHBRIDGE_LOG_LEVEL", value = "INFO" },
    { name = "RECEIVER_URL", value = local.receiver_url },
  ]

  common_secrets = concat(
    [
      { name = "LAUNCHBRIDGE_DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
      { name = "LAUNCHBRIDGE_WEBHOOK_SECRETS", valueFrom = aws_secretsmanager_secret.webhook_secrets.arn },
      { name = "LAUNCHBRIDGE_ADMIN_API_KEYS", valueFrom = aws_secretsmanager_secret.admin_api_keys.arn },
    ],
    [
      for key, secret in aws_secretsmanager_secret.destination_secrets :
      { name = "${upper(key)}_SECRET", valueFrom = secret.arn }
    ],
  )

  log_configuration = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.this.name
      awslogs-region        = var.region
      awslogs-stream-prefix = "app"
    }
  }
}

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name             = "migrate"
      image            = local.image
      essential        = true
      command          = ["alembic", "upgrade", "head"]
      environment      = local.common_environment
      secrets          = local.common_secrets
      logConfiguration = local.log_configuration
    }
  ])
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name         = "api"
      image        = local.image
      essential    = true
      portMappings = [{ containerPort = 8080, protocol = "tcp" }]
      environment  = local.common_environment
      secrets      = local.common_secrets
      healthCheck = {
        command     = ["CMD-SHELL", "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')\""]
        interval    = 15
        timeout     = 5
        retries     = 3
        startPeriod = 20
      }
      logConfiguration = local.log_configuration
    }
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.image
      essential = true
      command   = ["python", "-m", "launchbridge.worker"]
      environment = concat(local.common_environment, [
        { name = "LAUNCHBRIDGE_WORKER_CONCURRENCY", value = "16" },
      ])
      secrets          = local.common_secrets
      logConfiguration = local.log_configuration
    }
  ])
}

resource "aws_ecs_task_definition" "receiver" {
  count                    = var.deploy_receiver_fake ? 1 : 0
  family                   = "${var.name}-receiver"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name         = "receiver"
      image        = local.image
      essential    = true
      command      = ["uvicorn", "fakes.receiver:app", "--host", "0.0.0.0", "--port", "8081"]
      portMappings = [{ containerPort = 8081, protocol = "tcp" }]
      environment = [
        { name = "RECEIVER_SECRETS", value = join(",", [for k, v in var.destination_secrets : "${k}=${v}"]) },
      ]
      logConfiguration = local.log_configuration
    }
  ])
}

resource "aws_ecs_service" "api" {
  name                              = "${var.name}-api"
  cluster                           = aws_ecs_cluster.this.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.api_desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 30

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "worker" {
  name            = "${var.name}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }
}

resource "aws_ecs_service" "receiver" {
  count           = var.deploy_receiver_fake ? 1 : 0
  name            = "${var.name}-receiver"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.receiver[0].arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }

  service_registries {
    registry_arn = aws_service_discovery_service.receiver[0].arn
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.receiver[0].arn
    container_name   = "receiver"
    container_port   = 8081
  }

  depends_on = [aws_lb_listener.http]
}
