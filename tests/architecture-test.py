#!/usr/bin/env python3
"""The gate: the architecture artifact of prd carries what the judgment layer decides.

Run after `cexec aac-tools gen-architecture --stage prd --producer pilot-deploy`, in one of two
modes:

- plain, after a run against the published dataset, as AaC/PilotDeploy generates: whether or not
  the `pilot-app` producer is published, the four containers are the pod's instances, the gateway's
  specializes the estate's product, and pilot.ginbov.nl is the one exposed host, a web UI, on the
  UI's service and never on the gateway's;
- `--resolved`, after a run with PilotApp's model overlaid (ARCH_DATASET_OVERLAY): every mapping
  resolves, the host is on the UI's service, and the backend is Served by the database pooler,
  Keycloak and the pod's gateway, the migrations by the pooler.

gen-architecture reports a product or a recipe it cannot resolve as a gap and exits 0, so this is
what fails.
"""

import argparse
import pathlib
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
PRODUCER = "pilot-deploy"
ARTIFACT = REPO / "docs" / "architecture" / f"{PRODUCER}.yaml"
WORKLOAD = "pilot"
PUBLIC_HOST = "pilot.ginbov.nl"
# Container → the product its instance specializes. app:* of pilot are the `pilot-app` producer's.
PRODUCTS = {
    "migrate": "app:pilot",
    "backend": "app:pilot",
    "ui": "app:pilot-ui",
    "sse-gateway": "app:ssegateway",
}
# The UI's service, the `pilot-app` producer's, and the gateway's, which the host must never take.
UI_SERVICE = "svc:pilot-ui-web"
GATEWAY_SERVICE = "svc:ssegateway"
# The providers config/prd/values.yaml points the backend at: db.host's pooler and the Service
# behind auth.ginbov.nl, as the published dataset names their instances.
POOLER = "ss:postgres-pas-prd-postgres-pooler-rw"
KEYCLOAK = "ss:keycloak-prd-keycloak-keycloak"

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def bare(element_id):
    return element_id.split(",", 1)[0]


def resolved(element_id):
    # A composite id carries its UUID; gen-architecture keeps a reference it cannot resolve bare.
    return "," in element_id


def load():
    if not ARTIFACT.is_file():
        sys.exit(f"{ARTIFACT.relative_to(REPO)} is missing: run gen-architecture first")
    return yaml.safe_load(ARTIFACT.read_text())


def elements(doc):
    return [e for key, value in doc.items() if key != "relations" and isinstance(value, list) for e in value]


def relations(doc, kind, source=None, target=None):
    return [
        r
        for r in doc.get("relations") or []
        if r["type"] == kind
        and (source is None or r["source"] == source)
        and (target is None or r["target"] == target)
    ]


def instances(doc):
    by_container = {}
    for element in elements(doc):
        stats = element.get("stats") or {}
        if "container" in stats:
            check(
                stats.get("workload") == WORKLOAD,
                f"container {stats['container']} runs in workload {stats.get('workload')}, not {WORKLOAD}",
            )
            by_container[stats["container"]] = element["id"]
    return by_container


def check_products(doc, inst, must_resolve):
    check(doc.get("producer") == PRODUCER, f"the artifact's producer is {doc.get('producer')}, not {PRODUCER}")
    check(
        set(inst) == set(PRODUCTS),
        f"the artifact's instances are the containers {sorted(inst)}, not {sorted(PRODUCTS)}",
    )
    for container, product in PRODUCTS.items():
        if container not in inst:
            continue
        targets = [r["target"] for r in relations(doc, "Specialization", source=inst[container])]
        check(
            [bare(t) for t in targets] == [product],
            f"container {container} specializes {targets}, not {product}",
        )
        if must_resolve or product == "app:ssegateway":
            check(
                all(resolved(t) for t in targets),
                f"container {container}'s product {targets} resolves to no producer",
            )


def check_public_host(doc, inst, must_resolve):
    exposed = [
        e
        for e in doc.get("applicationInterfaces") or []
        if not (e.get("stats") or {}).get("url", "").endswith(".svc")
    ]
    check(
        [(e.get("stats") or {}).get("url") for e in exposed] == [PUBLIC_HOST],
        f"the exposed hosts are {[(e.get('stats') or {}).get('url') for e in exposed]}, not [{PUBLIC_HOST}]",
    )
    for interface in exposed:
        check(interface.get("webUi") is True, f"{PUBLIC_HOST} is not marked webUi")
        services = [r["target"] for r in relations(doc, "Assignment", source=interface["id"])]
        check(len(services) == 1, f"{PUBLIC_HOST} is assigned to {services}, not one service")
        for service in services:
            check(bare(service) != GATEWAY_SERVICE, f"{PUBLIC_HOST} is assigned to the gateway's {service}")
            if must_resolve:
                check(
                    bare(service) == UI_SERVICE and resolved(service),
                    f"{PUBLIC_HOST} is assigned to {service}, not {UI_SERVICE}",
                )
            elif bare(service) != UI_SERVICE:
                # The UI's product unpublished: the service is minted here, the UI's alone.
                realizers = [r["source"] for r in relations(doc, "Realization", target=service)]
                check(
                    realizers == [inst.get("ui")],
                    f"{PUBLIC_HOST}'s service {service} is realized by {realizers}, not the UI's instance",
                )


def check_served(doc, inst):
    def sources(container):
        return {bare(r["source"]) for r in relations(doc, "Serving", target=inst.get(container))}

    wanted = {
        "backend": {POOLER: "DATABASE_URL", KEYCLOAK: "OIDC_ISSUER_URL", bare(inst.get("sse-gateway", "")): "SSE_GATEWAY_URL"},
        "migrate": {POOLER: "DATABASE_URL"},
    }
    for container, providers in wanted.items():
        if container not in inst:
            continue
        for provider, var in providers.items():
            check(
                provider in sources(container),
                f"{container} is not Served by {provider}: its {var} recipe is unresolved",
            )
    check(KEYCLOAK not in sources("migrate"), f"migrate is Served by {KEYCLOAK}, but sets no OIDC_ISSUER_URL")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--resolved",
        action="store_true",
        help="the artifact was generated with PilotApp's model overlaid: every mapping resolves",
    )
    args = parser.parse_args()

    doc = load()
    inst = instances(doc)
    check_products(doc, inst, args.resolved)
    check_public_host(doc, inst, args.resolved)
    if args.resolved:
        check_served(doc, inst)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        sys.exit(1)
    print(f"ok: {PUBLIC_HOST} is the UI's{', and every mapping resolves' if args.resolved else ''}")


main()
