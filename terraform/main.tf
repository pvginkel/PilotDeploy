# Pilot's database and its login role on the shared postgres-pas substrate, and the connection
# Secret the backend reads in the app namespace (keys database, username, password); in a stage
# that asks for it, a read-only login role on that database and its own Secret beside it.

# Keeper name to value, set in config/<stage>/rotation.tfvars by secret-rotator's `terraform`
# kind. A keeper's name is the label of the block whose credential it re-mints. A keeper never
# set passes null, which leaves the credential as it is; removing a keeper once set re-mints the
# credential, so a keeper stays in the file.
variable "rotation_epoch" {
  description = "Rotation keepers: a changed value re-mints that block's credential."
  type        = map(string)
  default     = {}
  nullable    = false
}

module "db" {
  source = "./modules/postgres-db"

  name              = "db"
  namespace         = var.namespace
  secret_name       = "pilot-db"
  password_rotation = lookup(var.rotation_epoch, "db", null)
}

# The read-only role, <namespace>_readonly, for reading the database from outside the app (the
# operator's ad-hoc tools); its credentials go into the Secret pilot-db-readonly, which the
# backend does not read. Its default privileges are the app role's, which runs the migrations.
module "readonly" {
  source = "./modules/postgres-readonly-role"
  count  = var.readonly_role ? 1 : 0

  name              = "readonly"
  namespace         = var.namespace
  database          = module.db.database
  owner             = module.db.username
  secret_name       = "pilot-db-readonly"
  password_rotation = lookup(var.rotation_epoch, "readonly", null)
}
