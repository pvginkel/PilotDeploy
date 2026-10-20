# config/dev/ plans Pilot's dev database, its role and the connection Secret, and leaves the
# repository's webhook to prd, which owns it. Run by tests/terraform-test.py with
# config/dev/*.tfvars; the database's resources sit inside the postgres-db module, out of an
# assert's reach, so that script reads them from run "db"'s plan.

mock_provider "github" {}
mock_provider "kubernetes" {}
mock_provider "postgresql" {}
mock_provider "random" {}

# What the hook's environment supplies, invented.
variables {
  namespace               = "pilot-dev"
  postgres_admin_host     = "postgres-admin.test"
  postgres_admin_password = "test-admin-password"
  github_webhook_secret   = "shared-webhook-secret"
}

run "db" {
  command = plan

  assert {
    condition     = module.db.database == "pilot_dev_db" && module.db.username == "pilot_dev_db"
    error_message = "dev's database and role are not both pilot_dev_db, the substrate-wide <namespace>_<name>"
  }

  assert {
    condition     = module.db.secret_name == "pilot-db"
    error_message = "dev's connection Secret is not pilot-db, the one the backend reads"
  }
}

run "webhook" {
  command = plan

  assert {
    condition     = length(github_repository_webhook.argocd) == 0
    error_message = "dev plans the repository's webhook, which prd alone owns"
  }
}
