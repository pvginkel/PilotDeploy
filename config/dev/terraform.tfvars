# prd owns the repository's webhook (config/prd/terraform.tfvars); no other stage may.
manage_webhook = false
# The read-only role is prd's alone (config/prd/terraform.tfvars).
readonly_role = false
