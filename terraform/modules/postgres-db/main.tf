# A login role and its database on the shared postgres-pas substrate, plus
# the in-namespace Secret the chart consumes (keys: database/username/
# password). One module call = one database; a chart that needs several
# calls the module once per database with a distinct `name`.
#
# The role and database are the data-bearing resources: prevent_destroy is
# hardcoded (Terraform forbids parameterizing lifecycle). A deliberate
# destroy is a two-step — commit a temporary edit lifting the flag here,
# destroy, revert. The role is granted to pgadmin_admin so the maintenance UI
# inherits full access.
#
# Database and role are named <namespace>_<name> (underscores — the
# substrate is shared across namespaces and stages, so the namespace prefix
# keeps identifiers globally unique). `name` is required and has no default:
# the database a release provisions must be spelled out at the call site.

terraform {
  required_providers {
    postgresql = {
      source = "cyrilgdn/postgresql"
    }
    kubernetes = {
      source = "hashicorp/kubernetes"
    }
    random = {
      source = "hashicorp/random"
    }
  }
}

variable "name" {
  description = "Database short-name. The role and database are <namespace>_<name> (underscores). Required and visible — a chart with multiple databases calls the module once per name."
  type        = string
}

variable "namespace" {
  description = "Release namespace (<chart>-<stage>). Prefixes the identifier and is where the credentials Secret is written."
  type        = string
}

variable "secret_name" {
  description = "Name of the credentials Secret the chart consumes (keys: database/username/password)."
  type        = string
}

locals {
  identifier = "${replace(var.namespace, "-", "_")}_${var.name}"
}

resource "random_password" "this" {
  length  = 32
  special = false # URL-safe
}

resource "postgresql_role" "this" {
  name     = local.identifier
  login    = true
  password = random_password.this.result

  lifecycle {
    prevent_destroy = true
  }
}

resource "postgresql_database" "this" {
  name     = local.identifier
  owner    = postgresql_role.this.name
  template = "template0"

  lifecycle {
    prevent_destroy = true
  }
}

resource "postgresql_grant_role" "pgadmin" {
  role       = "pgadmin_admin"
  grant_role = postgresql_role.this.name
}

resource "kubernetes_secret_v1" "this" {
  metadata {
    name      = var.secret_name
    namespace = var.namespace
  }

  data = {
    database = postgresql_database.this.name
    username = postgresql_role.this.name
    password = random_password.this.result
  }
}

output "database" {
  value = postgresql_database.this.name
}

output "username" {
  value = postgresql_role.this.name
}

output "secret_name" {
  value = kubernetes_secret_v1.this.metadata[0].name
}
