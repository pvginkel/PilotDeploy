# prd owns the repository's webhook; no other stage may (terraform/variables.tf).
manage_webhook = true
# prd alone has the database's read-only role, pilot_prd_readonly (terraform/main.tf).
readonly_role = true
