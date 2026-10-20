#!/usr/bin/env python3
"""The repo's Terraform tests, on top of the shared `tf-check`, which runs first and leaves
terraform/ initialised.

For each stage, config/<stage>/*.tfvars, passed as the hook passes them, plan what
terraform/tests/<stage>.tftest.hcl asserts on. Its asserts reach only the root module's resources
and the postgres-db module's outputs, so this script also reads the plan
`terraform test -json -verbose` prints for run "db" and asserts on the module's role, database,
grant and Secret, and on the webhook planned in its owning stage alone. No backend and no
credentials: `terraform test` keeps its state in memory and mock providers answer every call.
"""

import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
STAGES = ("prd", "dev")
# The webhook is per repository and the state per stage, so exactly one stage owns it
# (terraform/variables.tf, manage_webhook).
WEBHOOK_OWNER = "prd"
# terraform/main.tf's module "db" writes the role and the database as the substrate-wide
# <namespace>_<name>, pilot_<stage>_db, and the Secret the backend reads them from.
SECRET = "pilot-db"
SECRET_KEYS = {"database", "username", "password"}
# The substrate's maintenance role, which inherits the app role's access.
MAINTENANCE_ROLE = "pgadmin_admin"
PASSWORD = "module.db.random_password.this"
ROLE = "module.db.postgresql_role.this"
DATABASE = "module.db.postgresql_database.this"
GRANT = "module.db.postgresql_grant_role.pgadmin"
CONNECTION = "module.db.kubernetes_secret_v1.this"
WEBHOOK = "github_repository_webhook.argocd[0]"

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def run_tests(stage):
    tfvars = sorted((REPO / "config" / stage).glob("*.tfvars"))
    args = [
        "terraform", "-chdir=terraform", "test", "-no-color", "-json", "-verbose",
        f"-filter=tests/{stage}.tftest.hcl",
        *(f"-var-file=../{path.relative_to(REPO)}" for path in tfvars),
    ]
    result = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
    messages = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    if result.returncode != 0:
        for message in messages:
            if message["type"] in ("diagnostic", "test_run", "test_summary"):
                print(message["@message"], file=sys.stderr)
                detail = (message.get("diagnostic") or {}).get("detail")
                if detail:
                    print(f"  {detail}", file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        sys.exit(f"terraform test of {stage} failed ({result.returncode})")
    return messages


def plan_of(messages, stage, run):
    for message in messages:
        if message["type"] == "test_plan" and message.get("@testrun") == run:
            return {
                change["address"]: change["change"]
                for change in message["test_plan"]["resource_changes"]
            }
    sys.exit(f"terraform test of {stage} printed no plan for run {run!r}")


def after(plan, address):
    return (plan.get(address) or {}).get("after") or {}


def check_db(plan, stage):
    identifier = f"pilot_{stage}_db"
    namespace = f"pilot-{stage}"
    owned = {WEBHOOK: ["create"]} if stage == WEBHOOK_OWNER else {}
    check(
        {address: change["actions"] for address, change in plan.items()}
        == {
            PASSWORD: ["create"],
            ROLE: ["create"],
            DATABASE: ["create"],
            GRANT: ["create"],
            CONNECTION: ["create"],
            **owned,
        },
        f"{stage} plans {sorted(plan)}, not the database, its role, password, grant and Secret"
        + (" and the webhook" if owned else "") + " alone",
    )
    password = after(plan, PASSWORD)
    check(
        password.get("length") == 32 and password.get("special") is False,
        f"{stage}'s role password is {password.get('length')} characters with special "
        f"{password.get('special')}, not 32 URL-safe ones",
    )
    role = after(plan, ROLE)
    check(
        role.get("name") == identifier and role.get("login") is True,
        f"{stage}'s role is {role.get('name')} with login {role.get('login')}, not the login role "
        f"{identifier}",
    )
    database = after(plan, DATABASE)
    check(
        database.get("name") == identifier
        and database.get("owner") == identifier
        and database.get("template") == "template0",
        f"{stage}'s database is {database.get('name')} owned by {database.get('owner')} from "
        f"{database.get('template')}, not {identifier} owned by its role from template0",
    )
    grant = after(plan, GRANT)
    check(
        grant.get("role") == MAINTENANCE_ROLE and grant.get("grant_role") == identifier,
        f"{stage} grants {grant.get('grant_role')} to {grant.get('role')}, not {identifier} to "
        f"{MAINTENANCE_ROLE}",
    )
    connection = plan.get(CONNECTION) or {}
    metadata = (after(plan, CONNECTION).get("metadata") or [{}])[0]
    check(
        metadata.get("name") == SECRET and metadata.get("namespace") == namespace,
        f"{stage} writes the connection Secret {metadata.get('namespace')}/{metadata.get('name')}, "
        f"not {namespace}/{SECRET}",
    )
    data = after(plan, CONNECTION).get("data") or {}
    unknown = (connection.get("after_unknown") or {}).get("data") or {}
    check(
        set(data) | set(unknown) == SECRET_KEYS,
        f"{stage}'s connection Secret holds {sorted(set(data) | set(unknown))}, not "
        f"{sorted(SECRET_KEYS)}",
    )
    check(
        data.get("database") == identifier and data.get("username") == identifier,
        f"{stage}'s connection Secret names database {data.get('database')} and user "
        f"{data.get('username')}, not {identifier}",
    )
    # Unknown at plan: the value is the generated password, not one written in the config.
    check(
        unknown.get("password") is True,
        f"{stage}'s connection Secret carries a password known at plan, not the generated one",
    )


def main():
    for stage in STAGES:
        check_db(plan_of(run_tests(stage), stage, "db"), stage)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        sys.exit(1)
    for stage in STAGES:
        webhook = " and the webhook" if stage == WEBHOOK_OWNER else ", and no webhook"
        print(f"ok: {stage} plans the database pilot_{stage}_db, its Secret {SECRET}{webhook}")


main()
