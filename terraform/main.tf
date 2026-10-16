# Pilot's database and its login role on the shared postgres-pas substrate, and the connection
# Secret the backend reads in the app namespace (keys database, username, password).

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
