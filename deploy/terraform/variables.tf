variable "name" {
  description = "Resource name prefix"
  type        = string
  default     = "launchbridge"
}

variable "environment" {
  description = "Environment label used in tags and names"
  type        = string
  default     = "trial"
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "image_tag" {
  description = "Image tag (git sha) pushed to the ECR repository"
  type        = string
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "deploy_receiver_fake" {
  description = "Deploy the receiver fake as an internal service so the smoke suite can run end to end"
  type        = bool
  default     = true
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "db_allocated_storage" {
  type    = number
  default = 20
}

variable "webhook_secrets" {
  description = "Map of inbound source name to HMAC secret"
  type        = map(string)
  sensitive   = true
}

variable "admin_api_keys" {
  description = "Map of label to admin API key"
  type        = map(string)
  sensitive   = true
}

variable "destination_secrets" {
  description = "Map of destination name to outbound HMAC secret; keys become env vars <NAME>_SECRET"
  type        = map(string)
  sensitive   = true
  default = {
    crm     = "replace-me"
    billing = "replace-me"
  }
}

variable "log_retention_days" {
  type    = number
  default = 14
}
