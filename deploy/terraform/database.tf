resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-db"
  subnet_ids = aws_subnet.database[*].id
}

resource "aws_db_instance" "this" {
  identifier                 = "${var.name}-${var.environment}"
  engine                     = "postgres"
  engine_version             = "16"
  instance_class             = var.db_instance_class
  allocated_storage          = var.db_allocated_storage
  storage_encrypted          = true
  db_name                    = "launchbridge"
  username                   = "launchbridge"
  password                   = random_password.db.result
  db_subnet_group_name       = aws_db_subnet_group.this.name
  vpc_security_group_ids     = [aws_security_group.database.id]
  multi_az                   = false
  publicly_accessible        = false
  backup_retention_period    = 7
  auto_minor_version_upgrade = true
  deletion_protection        = false
  skip_final_snapshot        = true
  apply_immediately          = true
}

locals {
  database_url = "postgresql+psycopg://${aws_db_instance.this.username}:${random_password.db.result}@${aws_db_instance.this.address}:${aws_db_instance.this.port}/${aws_db_instance.this.db_name}"
}
