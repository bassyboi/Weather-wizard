terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}
provider "aws" {
  region = var.aws_region
}

locals {
  name = "${var.project}-${var.env}"
}

resource "aws_ecr_repository" "api" {
  name                 = "${var.project}/api"
  image_tag_mutability = "MUTABLE"
  image_scanning_configuration { scan_on_push = true }
}

resource "aws_s3_bucket" "data" {
  bucket = "${var.project}-${var.env}-data"
  force_destroy = false
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration { status = "Enabled" }
}

output "ecr_api_repo_url" { value = aws_ecr_repository.api.repository_url }
output "s3_data_bucket"   { value = aws_s3_bucket.data.bucket }
