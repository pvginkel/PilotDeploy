# A read-only login role on a database the postgres-db module made, plus the in-namespace Secret
# that carries its credentials (keys: database/username/password). The role connects to the
# database and reads every table and sequence in its schema `public`, those a later migration
# creates included; it writes nothing and creates nothing.
#
# The role is named <namespace>_<name> (underscores), as postgres-db names its own. It holds no
# data, so it carries no prevent_destroy: dropping it loses nothing but the access.
#
# Every grant is made on objects the database's owning role owns. Terraform's role is not a
# superuser, so the provider takes that role's membership for the length of each grant and gives
# it back; it can, because it created the role.

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
  description = "The role's short-name. The role is <namespace>_<name> (underscores)."
  type        = string
}

variable "namespace" {
  description = "Release namespace (<chart>-<stage>). Prefixes the identifier and is where the credentials Secret is written."
  type        = string
}

variable "database" {
  description = "The database the role reads, a postgres-db module's."
  type        = string
}

variable "owner" {
  description = "The role that owns the database's objects and runs its migrations, the postgres-db module's login role. The default privileges are its: a table or sequence it creates later is readable too."
  type        = string
}

variable "secret_name" {
  description = "Name of the credentials Secret (keys: database/username/password)."
  type        = string
}

variable "password_rotation" {
  description = "Opaque rotation trigger. Any change re-mints the password on the same role, a set value dropped to null included; null while never set leaves it as it is."
  type        = string
  default     = null
}

locals {
  identifier = "${replace(var.namespace, "-", "_")}_${var.name}"
  schema     = "public"
}

resource "random_password" "this" {
  length  = 32
  special = false # URL-safe

  keepers = var.password_rotation == null ? null : { rotation = var.password_rotation }
}

resource "postgresql_role" "this" {
  name     = local.identifier
  login    = true
  password = random_password.this.result
}

resource "postgresql_grant" "connect" {
  database    = var.database
  role        = postgresql_role.this.name
  object_type = "database"
  privileges  = ["CONNECT"]
}

resource "postgresql_grant" "usage" {
  database    = var.database
  role        = postgresql_role.this.name
  schema      = local.schema
  object_type = "schema"
  privileges  = ["USAGE"]
}

# Every table and sequence the schema holds today.
resource "postgresql_grant" "tables" {
  database    = var.database
  role        = postgresql_role.this.name
  schema      = local.schema
  object_type = "table"
  privileges  = ["SELECT"]
}

resource "postgresql_grant" "sequences" {
  database    = var.database
  role        = postgresql_role.this.name
  schema      = local.schema
  object_type = "sequence"
  privileges  = ["SELECT"]
}

# Every table and sequence the owner creates in the schema from now on.
resource "postgresql_default_privileges" "tables" {
  database    = var.database
  role        = postgresql_role.this.name
  owner       = var.owner
  schema      = local.schema
  object_type = "table"
  privileges  = ["SELECT"]
}

resource "postgresql_default_privileges" "sequences" {
  database    = var.database
  role        = postgresql_role.this.name
  owner       = var.owner
  schema      = local.schema
  object_type = "sequence"
  privileges  = ["SELECT"]
}

resource "kubernetes_secret_v1" "this" {
  metadata {
    name      = var.secret_name
    namespace = var.namespace
  }

  data = {
    database = var.database
    username = postgresql_role.this.name
    password = random_password.this.result
  }
}

output "username" {
  value = postgresql_role.this.name
}

output "secret_name" {
  value = kubernetes_secret_v1.this.metadata[0].name
}
