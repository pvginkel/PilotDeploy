# Pilot's database and its login role on the shared postgres-pas substrate, and the connection
# Secret the backend reads in the app namespace (keys database, username, password).

module "db" {
  source = "./modules/postgres-db"

  name        = "db"
  namespace   = var.namespace
  secret_name = "pilot-db"
}
