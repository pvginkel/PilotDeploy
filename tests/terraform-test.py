#!/usr/bin/env python3
"""The repo's Terraform tests, on top of the shared `tf-check`, which runs first and leaves
terraform/ initialised.

For each stage, config/<stage>/*.tfvars, passed as the hook passes them, plan what
terraform/tests/<stage>.tftest.hcl asserts on. Its asserts reach only the root module's resources
and the postgres-db module's outputs, so this script also reads the plan
`terraform test -json -verbose` prints for run "db" and asserts on the module's role, database,
grant and Secret, on the read-only role, its grants and its Secret, planned in prd alone, and on
the webhook planned in its owning stage alone. No backend and no credentials: `terraform test`
keeps its state in memory and mock providers answer every call.
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
# terraform/main.tf's module "readonly", in prd alone (config/<stage>/terraform.tfvars,
# readonly_role): the login role pilot_prd_readonly, which reads every table and sequence of the
# database's schema public, now and as the app role creates them, and its own Secret.
READONLY_STAGES = {"prd"}
READONLY_SECRET = "pilot-db-readonly"
READONLY = "module.readonly[0]"
READONLY_PASSWORD = f"{READONLY}.random_password.this"
READONLY_ROLE = f"{READONLY}.postgresql_role.this"
READONLY_CONNECTION = f"{READONLY}.kubernetes_secret_v1.this"
# Each grant's address, object type and privileges: CONNECT, USAGE and SELECT, nothing else.
READONLY_GRANTS = {
    f"{READONLY}.postgresql_grant.connect": ("database", ["CONNECT"]),
    f"{READONLY}.postgresql_grant.usage": ("schema", ["USAGE"]),
    f"{READONLY}.postgresql_grant.tables": ("table", ["SELECT"]),
    f"{READONLY}.postgresql_grant.sequences": ("sequence", ["SELECT"]),
}
READONLY_DEFAULTS = {
    f"{READONLY}.postgresql_default_privileges.tables": ("table", ["SELECT"]),
    f"{READONLY}.postgresql_default_privileges.sequences": ("sequence", ["SELECT"]),
}

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
    readonly = (
        {
            address: ["create"]
            for address in (
                READONLY_PASSWORD,
                READONLY_ROLE,
                READONLY_CONNECTION,
                *READONLY_GRANTS,
                *READONLY_DEFAULTS,
            )
        }
        if stage in READONLY_STAGES
        else {}
    )
    check(
        {address: change["actions"] for address, change in plan.items()}
        == {
            PASSWORD: ["create"],
            ROLE: ["create"],
            DATABASE: ["create"],
            GRANT: ["create"],
            CONNECTION: ["create"],
            **readonly,
            **owned,
        },
        f"{stage} plans {sorted(plan)}, not the database, its role, password, grant and Secret"
        + (", the read-only role, its grants and Secret" if readonly else "")
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


def check_readonly(plan, stage):
    if stage not in READONLY_STAGES:
        return
    identifier = f"pilot_{stage}_db"
    role_name = f"pilot_{stage}_readonly"
    namespace = f"pilot-{stage}"
    password = after(plan, READONLY_PASSWORD)
    check(
        password.get("length") == 32 and password.get("special") is False,
        f"{stage}'s read-only password is {password.get('length')} characters with special "
        f"{password.get('special')}, not 32 URL-safe ones",
    )
    role = after(plan, READONLY_ROLE)
    elevated = [
        flag
        for flag in ("superuser", "create_database", "create_role", "replication")
        + ("bypass_row_level_security",)
        if role.get(flag)
    ]
    check(
        role.get("name") == role_name and role.get("login") is True and not elevated,
        f"{stage}'s read-only role is {role.get('name')} with login {role.get('login')} and "
        f"{elevated or 'no'} elevated attributes, not the plain login role {role_name}",
    )
    for address, (object_type, privileges) in {**READONLY_GRANTS, **READONLY_DEFAULTS}.items():
        grant = after(plan, address)
        check(
            grant.get("database") == identifier
            and grant.get("role") == role_name
            and grant.get("object_type") == object_type
            and sorted(grant.get("privileges") or []) == privileges
            and (object_type == "database" or grant.get("schema") == "public"),
            f"{stage}'s {address} grants {grant.get('privileges')} on {grant.get('object_type')} "
            f"in {grant.get('database')}/{grant.get('schema')} to {grant.get('role')}, not "
            f"{privileges} on {object_type} in {identifier} to {role_name}",
        )
        # A grant of every object leaves objects out; one naming objects would miss the rest.
        check(
            not grant.get("objects"),
            f"{stage}'s {address} names {grant.get('objects')}, not every {object_type}",
        )
    for address in READONLY_DEFAULTS:
        owner = after(plan, address).get("owner")
        check(
            owner == identifier,
            f"{stage}'s {address} is for objects {owner} creates, not {identifier}, the app role "
            f"the migrations run as",
        )
    metadata = (after(plan, READONLY_CONNECTION).get("metadata") or [{}])[0]
    check(
        metadata.get("name") == READONLY_SECRET and metadata.get("namespace") == namespace,
        f"{stage} writes the read-only Secret {metadata.get('namespace')}/{metadata.get('name')}, "
        f"not {namespace}/{READONLY_SECRET}",
    )
    data = after(plan, READONLY_CONNECTION).get("data") or {}
    unknown = ((plan.get(READONLY_CONNECTION) or {}).get("after_unknown") or {}).get("data") or {}
    check(
        set(data) | set(unknown) == SECRET_KEYS
        and data.get("database") == identifier
        and data.get("username") == role_name
        and unknown.get("password") is True,
        f"{stage}'s read-only Secret holds {sorted(set(data) | set(unknown))} naming database "
        f"{data.get('database')} and user {data.get('username')}, not {sorted(SECRET_KEYS)} naming "
        f"{identifier} and {role_name} with the generated password",
    )


def main():
    for stage in STAGES:
        plan = plan_of(run_tests(stage), stage, "db")
        check_db(plan, stage)
        check_readonly(plan, stage)
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        sys.exit(1)
    for stage in STAGES:
        webhook = " and the webhook" if stage == WEBHOOK_OWNER else ", and no webhook"
        readonly = (
            f", the read-only role pilot_{stage}_readonly and its Secret {READONLY_SECRET}"
            if stage in READONLY_STAGES
            else ", no read-only role"
        )
        print(
            f"ok: {stage} plans the database pilot_{stage}_db, its Secret {SECRET}"
            f"{readonly}{webhook}"
        )


main()
