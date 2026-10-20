# config/prd/ plans Pilot's database, its role and the connection Secret, and owns the
# repository's webhook, the one stage that does. Run by tests/terraform-test.py with
# config/prd/*.tfvars; the database's resources sit inside the postgres-db module, out of an
# assert's reach, so that script reads them from run "db"'s plan.

mock_provider "github" {}
mock_provider "kubernetes" {}
mock_provider "postgresql" {}
mock_provider "random" {}

# What the hook's environment supplies, invented.
variables {
  namespace               = "pilot-prd"
  postgres_admin_host     = "postgres-admin.test"
  postgres_admin_password = "test-admin-password"
  github_webhook_secret   = "shared-webhook-secret"
}

run "db" {
  command = plan

  assert {
    condition     = module.db.database == "pilot_prd_db" && module.db.username == "pilot_prd_db"
    error_message = "prd's database and role are not both pilot_prd_db, the substrate-wide <namespace>_<name>"
  }

  assert {
    condition     = module.db.secret_name == "pilot-db"
    error_message = "prd's connection Secret is not pilot-db, the one the backend reads"
  }
}

run "webhook" {
  command = plan

  assert {
    condition     = length(github_repository_webhook.argocd) == 1
    error_message = "prd, the stage that owns the repository's webhook, does not plan it"
  }

  assert {
    condition = (
      github_repository_webhook.argocd[0].repository == "PilotDeploy"
      && toset(github_repository_webhook.argocd[0].events) == toset(["push"])
      && github_repository_webhook.argocd[0].configuration[0].url == "https://deploy-hooks.webathome.org/api/webhook"
    )
    error_message = "prd's webhook does not deliver PilotDeploy's pushes to the webhook relay"
  }

  assert {
    condition     = github_repository_webhook.argocd[0].configuration[0].secret == var.github_webhook_secret
    error_message = "prd's webhook is not signed with Argo CD's shared webhook secret"
  }
}
