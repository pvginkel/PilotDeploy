#!/usr/bin/env python3
"""The gate: the pilot chart renders each stage, dev and prd, the way Argo CD renders it.

A stage is templated the way ArgoCDDeploy's registry has Argo produce it
(releases/templates/applications.yaml): `chart` under the Application's own name
`pilot-<stage>`, into that namespace, with `../config/<stage>/values.yaml` and the four `hook.*`
helm parameters of `releases.hookParameters` as `--set`. The library dependency is built from
chart/Chart.lock, as the repo-server builds it. Every directory under config/ is a stage this
gate knows.

Beyond the frame (the library, the pins, the namespace, the hook), it fails on what the render
shows would keep the pod from starting or put a secret in the commit: an env var reading a Secret
key nothing in the namespace provides, a `$(VAR)` the kubelet cannot resolve, a literal where a
secret belongs, the site's annotations off the stage's host and reach (public or internal), and the
backend's in-pod wiring (its drain) off the port and path the image uses. It fails too on a
container running the retired SSE gateway: the backend serves the browser's stream itself, and on
the links in text off the addresses the stage sets or set where values leave them out. It fails on
the backend's STAGE off the stage, its KubeCoder controller off the stage's own, its token off the
stage's leaf or handed to the migrations, or its CA file other than the homelab root mounted
read-only; on the pod's shutdown timing off the three values it is sized to; and on a stage's values
or render naming another stage's host, KubeCoder or leaves.

Run `cexec aac-tools chart-deps` first; the render needs chart/charts/ populated.
"""

import ast
import base64
import dataclasses
import pathlib
import re
import subprocess
import sys
import tarfile
import time
import urllib.parse

import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CHART = REPO / "chart"
# The front door's annotations on the Service in front of the UI (DockerImages
# nginx-configurator).
SERVER_NAME = "nginx.webathome.org/server-name"
IS_PUBLIC = "nginx.webathome.org/is-public"
ENABLE_SSL = "nginx.webathome.org/enable-ssl"
# The prd cluster's OpenBao, where each stage on that cluster has its leaves under
# eso/prd/pilot/<stage>/.
STORE = {"name": "openbao-prd", "kind": "ClusterSecretStore"}


@dataclasses.dataclass(frozen=True)
class Stage:
    name: str
    # The site the Service carries and the backend's BASEURL names.
    host: str
    # A public site's front door serves it with a Let's Encrypt certificate whatever enable-ssl
    # says, so it carries none. An internal one is served over HTTPS only with enable-ssl, from a
    # homelab step-ca certificate.
    public: bool
    # The KubeCoder controller the backend's client calls and an environment link opens in.
    kubecoder: str

    @property
    def namespace(self):
        # The Application's name and its destination namespace, one string (argo-cd D24).
        return f"pilot-{self.name}"

    @property
    def values(self):
        return f"config/{self.name}/values.yaml"

    @property
    def tag_pin(self):
        # `:<stage>-<n>`, the tag PilotApp's build pins, or a bare `:<n>`, which the registry
        # holds for its older builds.
        return re.compile(rf":(?:{re.escape(self.name)}-)?[0-9]+")

    @property
    def annotations(self):
        if self.public:
            return {SERVER_NAME: self.host, IS_PUBLIC: "true"}
        return {SERVER_NAME: self.host, IS_PUBLIC: "false", ENABLE_SSL: "yes"}

    @property
    def leaves(self):
        """The leaves the operator writes, each read by the backend's env var from the Secret an
        ExternalSecret fills: {var: (Secret, leaf, property)}."""
        oidc = f"eso/prd/pilot/{self.name}/oidc"
        kubecoder = f"eso/prd/pilot/{self.name}/kubecoder"
        return {
            "OIDC_CLIENT_ID": ("pilot-oidc", oidc, "client_id"),
            "OIDC_CLIENT_SECRET": ("pilot-oidc", oidc, "client_secret"),
            "CONTROLLER_TOKEN": ("pilot-kubecoder", kubecoder, "token"),
        }


# Production, the public site on the operator's own KubeCoder, and dev, internal beside it on the
# prd cluster with kubecoder-dev's fleet.
STAGES = (
    Stage("prd", host="pilot.ginbov.nl", public=True, kubecoder="https://kubecoder.home"),
    Stage("dev", host="pilot-dev.home", public=False, kubecoder="https://kubecoder-dev.home"),
)
# The architecture producer's id is the chart's name plus `-deploy`.
CHART_NAME = "pilot"
# Exact (D17), from the chart repository Argo's repo-server resolves it from.
LIBRARY = {"name": "homelab-shared", "version": "0.6.0", "repository": "https://charts.home"}
DEFINE = re.compile(r"\{\{-?\s*define\b")
# The registry entry's `repo:` form, and a SHA of the shape `$ARGOCD_APP_REVISION` becomes.
HOOK_REPO = "https://github.com/pvginkel/PilotDeploy.git"
HOOK_REVISION = "0123456789abcdef0123456789abcdef01234567"
# Where the library's Job template puts the hook (homelab-shared templates/_tf-presync-hook.tpl).
HOOK_NAMESPACE = "argocd-hooks"
HOOK_GUARD = "is required by the Terraform PreSync hook"
# The image slots of config/<stage>/values.yaml, which PilotApp's build writes
# (cicd.writeVersionPins, its Jenkinsfile's "Write image pins").
PINS = ("images.pilot", "images.pilotUI")
# A key line as cicd.writeVersionPins matches it (JenkinsPipelineUtils vars/cicd.groovy, applyPins).
KEY_LINE = re.compile(r" *([A-Za-z0-9_.\-]+): *(.*)")
# The one cluster-scoped kind the chart renders, which the releases AppProject's
# clusterResourceWhitelist carries (ArgoCDDeploy chart/templates/appproject.yaml).
CLUSTER_SCOPED = {("", "Namespace")}
# Namespaced kinds, as (group, kind). A render cannot tell a kind's scope, so a kind in neither
# set fails the gate until it is classified.
NAMESPACED = {
    ("", "ConfigMap"),
    ("", "Service"),
    ("apps", "Deployment"),
    ("batch", "Job"),
    ("external-secrets.io", "ExternalSecret"),
    ("generators.external-secrets.io", "Password"),
    ("rbac.authorization.k8s.io", "RoleBinding"),
}
# The pod: its Deployment, and the image each container runs, from which pin.
DEPLOYMENT = "pilot"
CONTAINER_IMAGES = {
    "migrate": ("registry:5000/pilot", "pilot"),
    "backend": ("registry:5000/pilot", "pilot"),
    "ui": ("registry:5000/pilot-ui", "pilotUI"),
}
# The SSE gateway's image, retired: the backend serves the browser's stream itself
# (GET /api/sse/stream, backend/app/api/sse.py), behind the UI's nginx `location /api/`.
GATEWAY_IMAGE = "ssegateway"
# PilotApp's contracts: the backend's port (backend/app/consts.py DEFAULT_BACKEND_PORT) and its
# health routes at the app root (backend/app/__init__.py), the UI's nginx port
# (frontend/nginx.conf), and the migration command (backend/app/cli.py, run as a module: the image
# installs no project scripts).
BACKEND_PORT = 3501
UI_PORT = 3500
BACKEND_PROBES = {"readinessProbe": "/health/readyz", "livenessProbe": "/health/healthz"}
MIGRATE_ARGS = ["python", "-m", "app.cli", "upgrade-db"]
# The pod's in-pod wiring, over loopback: the backend's preStop drains through /health/drain
# (backend/app/api/health.py) with a bearer from its own env.
DRAIN_PATH = "/health/drain"
LOOPBACK = ("localhost", "127.0.0.1")
# A URL in the preStop's Python, and an env var it reads.
SCRIPT_URL = re.compile(r"http://[^\s\"']+")
SCRIPT_ENV = re.compile(r"os\.environ\[[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']\]")
FRONT_DOOR = ("nginx.webathome.org/", "dns.webathome.org/")
# The internals fact (the copy-diagnostics button, a 500's exception text), and the sign-in, on the
# one Keycloak realm both stages' clients live in. No OIDC_AUDIENCE: the audience defaults to the
# client id, under whose resource_access the backend's editor gate reads the role. No SSE gateway
# settings: the backend serves the stream itself and reads neither (backend/app/config.py).
BACKEND_ENV = {
    "HOMELAB_INTERNAL": "true",
    "OIDC_ENABLED": "true",
    "OIDC_ISSUER_URL": "https://auth.ginbov.nl/realms/homelab",
}
ABSENT_ENV = ("OIDC_AUDIENCE", "SSE_GATEWAY_URL", "SSE_CALLBACK_SECRET")
# KubeCoder's controller (PilotApp backend/app/app_config.py): the stage's address, the token of
# Pilot's named client credential there, and CONTROLLER_CA_FILE, the homelab root the controller's
# certificate chains to, which the image does not trust. The migrations never call the controller,
# so the migrate init container sets none of the three.
CONTROLLER_VARS = ("CONTROLLER_URL", "CONTROLLER_TOKEN", "CONTROLLER_CA_FILE")
CA_FILE_ENV = "CONTROLLER_CA_FILE"
# The root's common name, which a self-signed certificate's DER carries twice: issuer and subject.
HOMELAB_ROOT_CN = b"homelab-ca Root CA"
PEM_CERTIFICATE = re.compile(
    r"-----BEGIN CERTIFICATE-----\n([A-Za-z0-9+/=\n]+)-----END CERTIFICATE-----\n?"
)
# The shutdown timing. Nothing drains to completion: every job stops within seconds and the next
# pod resumes it from its rows. So the lifecycle coordinator's budget for its waiters is 20 s, the
# preStop waits 40 s for the drain, above that budget with room for the SHUTDOWN handlers, and the
# grace covers the preStop's wait for the endpoints, its 40 s and 15 s for the process to exit.
SHUTDOWN_ENV = {"GRACEFUL_SHUTDOWN_TIMEOUT": "20"}
DRAIN_TIMEOUT = 40
GRACE_SECONDS = 70
# The preStop's Python: its wait for the endpoints, and the drain call's timeout.
SCRIPT_SLEEP = re.compile(r"time\.sleep\(([0-9]+)\)")
SCRIPT_TIMEOUT = re.compile(r"timeout=([0-9]+)")
# The links in text (PilotApp backend/app/app_config.py, F91, F93): a ticket's YouTrack issue,
# `{id}` where the id goes, and the stage's KubeCoder, whose <base>/environments/<env-id>/ssh/open
# opens an environment in VS Code. The values' `links` sets them; without it the backend has
# neither.
LINK_VALUES = "links"
TICKET_URL_TEMPLATE = "https://issues.webathome.org/issue/{id}"
LINK_VARS = ("TICKET_URL_TEMPLATE", "ENVIRONMENT_LINK_BASE_URL")
# The secrets ESO generates in the cluster, once each; a Password generator yields key `password`.
GENERATED = {"pilot-secret-key", "pilot-drain-auth"}
GENERATOR_KEY = "password"
# An env var or query parameter named like this carries a secret, so never a literal.
SECRET_NAME = re.compile(r"secret|password|token|(^|_)key$", re.IGNORECASE)
# A Kubernetes dependent-env reference; `$$(` escapes a literal `$(`.
ENV_REF = re.compile(r"(?<!\$)\$\(([A-Za-z_][A-Za-z0-9_]*)\)")
# Env vars two containers must define alike, references resolved: the migrations run against the
# database the backend serves from, under its secret key, stage and internals fact.
SHARED_ENV = {
    ("migrate", "backend"): ("DATABASE_URL", "SECRET_KEY", "STAGE", "HOMELAB_INTERNAL"),
}
# A bearer token written into the chart (ElectronicsInventoryDeploy's preStop header).
LITERAL_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}")

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def hook_parameters(stage):
    # In the library's argument order: repo, revision, stage, namespace.
    return {
        "hook.repo": HOOK_REPO,
        "hook.revision": HOOK_REVISION,
        "hook.stage": stage.name,
        "hook.namespace": stage.namespace,
    }


def template(stage, parameters):
    args = [
        "helm", "template", stage.namespace, "chart",
        "--namespace", stage.namespace,
        "--values", stage.values,
    ]
    for name, value in parameters.items():
        args += ["--set", f"{name}={value}"]
    return subprocess.run(args, cwd=REPO, capture_output=True, text=True)


def render(stage):
    result = template(stage, hook_parameters(stage))
    if result.returncode != 0:
        sys.exit(f"helm template of {stage.name} failed:\n{result.stderr}")
    return result.stdout


def objects(stdout):
    return [doc for doc in yaml.safe_load_all(stdout) if doc]


def find(docs, kind, name):
    for doc in docs:
        if doc["kind"] == kind and doc["metadata"].get("name") == name:
            return doc
    return None


def label(doc):
    return f"{doc['kind']}/{doc['metadata'].get('name')}"


def values_text(stage):
    return (REPO / stage.values).read_text()


def library_values():
    """The built library's own values.yaml, as chart-deps unpacked it from chart/Chart.lock."""
    archive = CHART / "charts" / f"{LIBRARY['name']}-{LIBRARY['version']}.tgz"
    with tarfile.open(archive) as tar:
        return yaml.safe_load(tar.extractfile(f"{LIBRARY['name']}/values.yaml"))


def pin_paths(text):
    """{dotted path: [line numbers]} of every key line cicd.writeVersionPins can rewrite: a
    block-style `key: value` line, its path the keys of the block lines above it. Comments,
    blank lines, block-scalar bodies and anything that is not a key line carry no path."""
    found = {}
    keys = []
    indents = []
    # Indent of an open block scalar (`key: |`); -1 when none is open.
    literal = -1
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if literal >= 0:
            if indent > literal:
                continue
            literal = -1
        match = KEY_LINE.fullmatch(line)
        if match is None:
            continue
        key, rest = match.groups()
        while indents and indents[-1] >= indent:
            indents.pop()
            keys.pop()
        if not rest or rest.startswith("#"):
            keys.append(key)
            indents.append(indent)
            continue
        if rest.startswith(("|", ">")):
            literal = indent
            continue
        found.setdefault(".".join([*keys, key]), []).append(number)
    return found


def check_library():
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    check(
        chart.get("name") == CHART_NAME,
        f"chart/Chart.yaml names the chart {chart.get('name')!r}, not {CHART_NAME!r}, from which "
        "the producer id pilot-deploy derives",
    )
    check(
        chart.get("dependencies") == [LIBRARY],
        f"chart/Chart.yaml does not pin exactly {LIBRARY}: {chart.get('dependencies')}",
    )
    links = sorted(str(p.relative_to(REPO)) for p in CHART.rglob("*") if p.is_symlink())
    check(not links, f"symlinks under chart/, which Argo's repo-server refuses: {links}")
    defines = sorted(
        p.name for p in (CHART / "templates").iterdir() if DEFINE.search(p.read_text())
    )
    check(not defines, f"templates define helpers the library chart provides: {defines}")
    tracked = subprocess.run(
        ["git", "ls-files", "chart/charts"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    check(not tracked, f"the built dependency is committed: {tracked}")


def check_chart_values():
    # A chart's own values.yaml is optional to Helm.
    path = CHART / "values.yaml"
    values = (yaml.safe_load(path.read_text()) if path.exists() else None) or {}
    check(
        "images" not in values,
        f"chart/values.yaml names images {values.get('images')}; the pins live in "
        "config/<stage>/values.yaml alone, with no chart default behind them",
    )


def check_stages():
    found = sorted(path.name for path in (REPO / "config").iterdir() if path.is_dir())
    check(
        found == sorted(stage.name for stage in STAGES),
        f"config/ holds the stages {found}, not the ones this gate renders "
        f"{sorted(stage.name for stage in STAGES)}",
    )


def check_pin_slots(stage):
    paths = pin_paths(values_text(stage))
    images = yaml.safe_load(values_text(stage)).get("images") or {}
    for pin in PINS:
        lines = paths.get(pin, [])
        check(
            len(lines) == 1,
            f"{stage.values} holds {pin} on lines {lines}, not on exactly one block-style key "
            "line, which cicd.writeVersionPins edits in place and never creates",
        )
        value = images.get(pin.removeprefix("images."))
        check(
            isinstance(value, str) and stage.tag_pin.fullmatch(value) is not None,
            f"{stage.values} pins {pin} to {value!r}, not a tag suffix ':{stage.name}-<build>' "
            "or ':<build>'",
        )
    stale = sorted(f"images.{name}" for name in images if f"images.{name}" not in PINS)
    check(not stale, f"{stage.values} pins {stale}, beyond the pod's slots {list(PINS)}")


def check_namespace_manifest(docs, stage):
    namespace = stage.namespace
    names = [doc["metadata"]["name"] for doc in docs if doc["kind"] == "Namespace"]
    check(names == [namespace], f"renders Namespaces {names}, not the one {namespace} (D25)")
    manifest = find(docs, "Namespace", namespace)
    if manifest is None:
        return
    annotations = manifest["metadata"].get("annotations") or {}
    check(
        annotations.get("argocd.argoproj.io/sync-wave") == "-1",
        f"Namespace/{namespace} is missing sync-wave -1 (D25)",
    )
    check(
        annotations.get("argocd.argoproj.io/sync-options") == "Prune=false",
        f"Namespace/{namespace} is missing sync-options Prune=false (D26)",
    )


def check_hook(docs, stage):
    jobs = [doc for doc in docs if doc["kind"] == "Job"]
    check(len(jobs) == 1, f"renders {len(jobs)} Jobs, not the one PreSync hook")
    if len(jobs) != 1:
        return
    metadata = jobs[0]["metadata"]
    namespace = stage.namespace
    check(
        metadata.get("name") == f"tf-presync-{namespace}"
        and metadata.get("namespace") == HOOK_NAMESPACE,
        f"the hook Job is {label(jobs[0])} in {metadata.get('namespace')}, "
        f"not tf-presync-{namespace} in {HOOK_NAMESPACE}",
    )
    check(
        (metadata.get("annotations") or {}).get("argocd.argoproj.io/hook") == "PreSync",
        "the hook Job is not a PreSync hook",
    )
    containers = jobs[0]["spec"]["template"]["spec"]["containers"]
    # hook.path is unset: the repo's root is the app, so the Job passes exactly four arguments.
    args = [c.get("args") for c in containers]
    expected = list(hook_parameters(stage).values())
    check(args == [expected], f"the hook Job's args are {args}, not {expected}")
    image = f"registry:5000/argocd-hook:{library_values()['hook']['imageTag']}"
    check(
        [c.get("image") for c in containers] == [image],
        f"the hook Job runs {[c.get('image') for c in containers]}, not the library's estate-wide "
        f"pin {image}",
    )

    binding = find(docs, "RoleBinding", "tf-presync")
    check(
        binding is not None and binding["metadata"].get("namespace") == namespace,
        f"no RoleBinding/tf-presync in {namespace}, the hook's grant there",
    )
    revision = find(docs, "ConfigMap", "tf-presync-revision")
    check(
        revision is not None
        and revision["metadata"].get("namespace") == namespace
        and revision["data"] == {"revision": HOOK_REVISION},
        f"no ConfigMap/tf-presync-revision in {namespace} carrying the synced revision (D67)",
    )

    for omitted in hook_parameters(stage):
        parameters = {k: v for k, v in hook_parameters(stage).items() if k != omitted}
        result = template(stage, parameters)
        check(
            result.returncode != 0 and f"{omitted} {HOOK_GUARD}" in result.stderr,
            f"rendering without {omitted} does not fail on the library's guard",
        )


def check_scope(docs, stage):
    for doc in docs:
        api_version = doc["apiVersion"]
        group_kind = (api_version.split("/")[0] if "/" in api_version else "", doc["kind"])
        if group_kind in CLUSTER_SCOPED:
            continue
        check(
            group_kind in NAMESPACED,
            f"{label(doc)} is a kind this gate has not classified; a cluster-scoped kind "
            "needs the releases AppProject to whitelist it first",
        )
        explicit = doc["metadata"].get("namespace")
        check(
            explicit in (None, HOOK_NAMESPACE, stage.namespace),
            f"{label(doc)} renders into {explicit}, outside the Application's destination",
        )


def terraform_secrets():
    """{name: keys} of the Secret Terraform writes into the namespace: the SECRET and SECRET_KEYS
    tests/terraform-test.py asserts the plan writes."""
    constants = {}
    for node in ast.parse((REPO / "tests" / "terraform-test.py").read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            try:
                constants[node.targets[0].id] = ast.literal_eval(node.value)
            except ValueError:
                continue
    return {constants["SECRET"]: set(constants["SECRET_KEYS"])}


def secret_providers(docs):
    """{Secret name: its keys} of every Secret the namespace gets: Terraform's, and each rendered
    ExternalSecret's target. None where a render cannot know the keys: a whole extracted leaf, or
    a generator the chart does not render."""
    passwords = {doc["metadata"]["name"] for doc in docs if doc["kind"] == "Password"}
    providers = terraform_secrets()
    for doc in docs:
        if doc["kind"] != "ExternalSecret":
            continue
        spec = doc["spec"]
        target = spec.get("target") or {}
        keys = {item["secretKey"] for item in spec.get("data") or []}
        for source in spec.get("dataFrom") or []:
            generator = (source.get("sourceRef") or {}).get("generatorRef") or {}
            if generator.get("kind") == "Password" and generator.get("name") in passwords:
                keys.add(GENERATOR_KEY)
            else:
                keys = None
                break
        template = (target.get("template") or {}).get("data")
        if keys is not None and template is not None:
            keys = set(template)
        providers[target.get("name") or doc["metadata"]["name"]] = keys
    return providers


def pod_containers(deployment):
    spec = deployment["spec"]["template"]["spec"]
    return [*(spec.get("initContainers") or []), *spec["containers"]]


def named(deployment, name):
    return next((c for c in pod_containers(deployment) if c["name"] == name), {})


def env_of(container):
    return {entry["name"]: (i, entry) for i, entry in enumerate(container.get("env") or [])}


def secret_ref(entry):
    return (entry.get("valueFrom") or {}).get("secretKeyRef")


def definition(env, name, before=None):
    """`name`'s definition in `env`, each $(VAR) it reads replaced by VAR's own definition as the
    kubelet resolves it: against the variables defined before it. None where it is undefined."""
    position, entry = env.get(name, (None, None))
    if entry is None or (before is not None and position >= before):
        return None
    if "value" not in entry:
        return entry.get("valueFrom")
    refs = ENV_REF.findall(entry["value"])
    return [entry["value"], {ref: definition(env, ref, position) for ref in refs}]


def container_port(container, port):
    """The containerPort a probe's or a Service's port names in `container`, or None."""
    for declared in container.get("ports") or []:
        if port in (declared.get("name"), declared["containerPort"]):
            return declared["containerPort"]
    return None


def backend_env(stage):
    """The backend's env values the stage decides, beside those it shares with every stage."""
    return {
        **BACKEND_ENV,
        "STAGE": stage.name,
        "BASEURL": f"https://{stage.host}",
        "TICKET_URL_TEMPLATE": TICKET_URL_TEMPLATE,
        "ENVIRONMENT_LINK_BASE_URL": stage.kubecoder,
        "CONTROLLER_URL": stage.kubecoder,
        **SHUTDOWN_ENV,
    }


def check_pod(docs, stage):
    deployments = [doc for doc in docs if doc["kind"] == "Deployment"]
    check(
        [label(doc) for doc in deployments] == [f"Deployment/{DEPLOYMENT}"],
        f"renders {[label(doc) for doc in deployments]}, not the one Deployment/{DEPLOYMENT}",
    )
    deployment = find(docs, "Deployment", DEPLOYMENT)
    if deployment is None:
        return None
    spec = deployment["spec"]["template"]["spec"]
    names = {
        "initContainers": [c["name"] for c in spec.get("initContainers") or []],
        "containers": [c["name"] for c in spec["containers"]],
    }
    check(
        names == {"initContainers": ["migrate"], "containers": ["backend", "ui"]},
        f"the pod runs {names}, not the migrations before the backend and the UI",
    )
    for name in CONTAINER_IMAGES:
        container = named(deployment, name)
        for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
            port = ((container.get(probe) or {}).get("httpGet") or {}).get("port")
            check(
                port is None or container_port(container, port) is not None,
                f"container {name}'s {probe} reads port {port!r}, which it does not declare",
            )

    migrate = named(deployment, "migrate")
    check(
        "command" not in migrate and migrate.get("args") == MIGRATE_ARGS,
        f"the migrate init container runs command {migrate.get('command')} args "
        f"{migrate.get('args')}, not {MIGRATE_ARGS} under the image's entrypoint",
    )
    backend = named(deployment, "backend")
    for probe, path in BACKEND_PROBES.items():
        get = (backend.get(probe) or {}).get("httpGet") or {}
        check(
            get.get("path") == path and container_port(backend, get.get("port")) == BACKEND_PORT,
            f"the backend's {probe} reads {get.get('path')} on {get.get('port')}, not {path} on "
            f"{BACKEND_PORT}",
        )
    env = env_of(backend)
    for name, value in backend_env(stage).items():
        entry = env.get(name, (None, {}))[1]
        check(
            entry.get("value") == value,
            f"the backend's {name} is {entry.get('value')!r}, not {value!r}",
        )
    for name in ABSENT_ENV:
        check(name not in env, f"the backend sets {name}")
    return deployment


def loopback(url, port, path):
    """Whether `url` reaches `path` on `port` of the pod's own loopback."""
    try:
        parts = urllib.parse.urlsplit(url)
        return (
            parts.scheme == "http"
            and parts.hostname in LOOPBACK
            and parts.port == port
            and parts.path == path
        )
    except ValueError:
        return False


def check_wiring(deployment):
    """The backend's preStop reaches its drain over loopback, on the port and path the image
    uses."""
    backend = named(deployment, "backend")
    command = " ".join(
        ((backend.get("lifecycle") or {}).get("preStop") or {}).get("exec", {}).get("command")
        or []
    )
    urls = SCRIPT_URL.findall(command)
    check(
        len(urls) == 1 and loopback(urls[0], BACKEND_PORT, DRAIN_PATH),
        f"the backend's preStop calls {urls}, not its {DRAIN_PATH} on localhost:{BACKEND_PORT}",
    )
    reads = SCRIPT_ENV.findall(command)
    defined = env_of(backend)
    check(
        reads and all(name in defined for name in reads),
        f"the backend's preStop reads env {reads}, of which the backend defines "
        f"{[name for name in reads if name in defined]}",
    )


def check_timing(deployment):
    """The grace and the preStop's drain timeout are the values the shutdown is sized to, and the
    grace covers everything the preStop may take."""
    spec = deployment["spec"]["template"]["spec"]
    check(
        spec.get("terminationGracePeriodSeconds") == GRACE_SECONDS,
        f"the pod's terminationGracePeriodSeconds is {spec.get('terminationGracePeriodSeconds')}, "
        f"not {GRACE_SECONDS}",
    )
    backend = named(deployment, "backend")
    command = " ".join(
        ((backend.get("lifecycle") or {}).get("preStop") or {}).get("exec", {}).get("command")
        or []
    )
    sleeps = [int(n) for n in SCRIPT_SLEEP.findall(command)]
    timeouts = [int(n) for n in SCRIPT_TIMEOUT.findall(command)]
    check(
        timeouts == [DRAIN_TIMEOUT],
        f"the backend's preStop waits {timeouts} s for its drain, not {DRAIN_TIMEOUT}",
    )
    check(
        len(sleeps) == 1 and sum(sleeps) + DRAIN_TIMEOUT < GRACE_SECONDS,
        f"the backend's preStop sleeps {sleeps} s and waits {DRAIN_TIMEOUT} s for its drain, "
        f"which the grace of {GRACE_SECONDS} s does not cover",
    )


def ca_certificate(text):
    """The DER of `text` when it is exactly one PEM certificate, else None."""
    match = PEM_CERTIFICATE.fullmatch(text)
    return base64.b64decode(match.group(1)) if match else None


def check_controller(docs, deployment):
    """The backend's CONTROLLER_CA_FILE is the homelab root, mounted read-only from a ConfigMap
    the chart renders; the migrations get none of the controller's settings."""
    migrate = env_of(named(deployment, "migrate"))
    present = sorted(name for name in migrate if name in CONTROLLER_VARS)
    check(
        not present,
        f"the migrate init container sets {present}, which the migrations never read",
    )

    backend = named(deployment, "backend")
    path = env_of(backend).get(CA_FILE_ENV, (None, {}))[1].get("value") or ""
    mounts = [
        mount for mount in backend.get("volumeMounts") or []
        if path.startswith(mount["mountPath"].rstrip("/") + "/")
    ]
    check(
        len(mounts) == 1,
        f"the backend's {CA_FILE_ENV} {path!r} lies under {len(mounts)} of its mounts, not one",
    )
    if len(mounts) != 1:
        return
    mount = mounts[0]
    check(mount.get("readOnly") is True, f"the backend mounts {mount['name']} writable")
    volumes = deployment["spec"]["template"]["spec"].get("volumes") or []
    source = next((v for v in volumes if v["name"] == mount["name"]), {}).get("configMap") or {}
    relative = path[len(mount["mountPath"].rstrip("/")) + 1:]
    items = {item["path"]: item["key"] for item in source.get("items") or []}
    key = items.get(relative) if items else relative
    config_map = find(docs, "ConfigMap", source.get("name")) or {}
    der = ca_certificate((config_map.get("data") or {}).get(key) or "")
    check(
        der is not None and der.count(HOMELAB_ROOT_CN) == 2,
        f"the backend's {CA_FILE_ENV} {path} is key {key} of the ConfigMap {source.get('name')}, "
        f"which is not one PEM certificate issued by and to {HOMELAB_ROOT_CN.decode()}",
    )


def image_name(image):
    """An image reference's repository name, without registry, path, tag or digest."""
    return image.split("@", 1)[0].rsplit("/", 1)[-1].split(":", 1)[0]


def check_no_gateway(docs):
    """No pod the chart renders runs the retired SSE gateway."""
    for doc in docs:
        spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
        for container in [*(spec.get("initContainers") or []), *(spec.get("containers") or [])]:
            check(
                image_name(container.get("image") or "") != GATEWAY_IMAGE,
                f"{label(doc)}'s container {container['name']} runs {container.get('image')}, the "
                "retired SSE gateway; the backend serves the stream itself",
            )


def check_images(stage):
    """Each container runs its own pin's image. The two app pins hold one build number, so the
    stage's render cannot tell them apart; this one sets each pin to a tag of its own."""
    names = sorted({pin for _, pin in CONTAINER_IMAGES.values()})
    pins = {pin: f":{9001 + i}" for i, pin in enumerate(names)}
    result = template(
        stage, {**hook_parameters(stage), **{f"images.{pin}": tag for pin, tag in pins.items()}}
    )
    if result.returncode != 0:
        sys.exit(f"helm template with distinct pins failed:\n{result.stderr}")
    deployment = find(objects(result.stdout), "Deployment", DEPLOYMENT) or {}
    for name, (repository, pin) in CONTAINER_IMAGES.items():
        image = named(deployment, name).get("image") if deployment else None
        check(
            image == f"{repository}{pins[pin]}",
            f"container {name} runs {image}, not images.{pin}'s {repository}{pins[pin]}",
        )


def check_links_unset(stage):
    """Without the values' links, no container has a link setting: the text shows unlinked."""
    result = template(stage, {**hook_parameters(stage), LINK_VALUES: "null"})
    if result.returncode != 0:
        sys.exit(f"helm template without {LINK_VALUES} failed:\n{result.stderr}")
    deployment = find(objects(result.stdout), "Deployment", DEPLOYMENT)
    check(deployment is not None, f"without {LINK_VALUES} in values, no Deployment/{DEPLOYMENT}")
    for container in pod_containers(deployment) if deployment else []:
        present = sorted(name for name in env_of(container) if name in LINK_VARS)
        check(
            not present,
            f"without {LINK_VALUES} in values, container {container['name']} sets {present}",
        )


def check_env(deployment):
    """Every container's env resolves as the kubelet resolves it, and no secret is a literal."""
    for container in pod_containers(deployment):
        name = container["name"]
        env = env_of(container)
        from_secrets = {var for var, (_, entry) in env.items() if secret_ref(entry)}
        for var, (position, entry) in env.items():
            if SECRET_NAME.search(var):
                check(
                    secret_ref(entry) is not None,
                    f"container {name}'s {var} is not read from a Secret: a literal where a "
                    "secret belongs",
                )
            if "value" not in entry:
                continue
            value = entry["value"]
            for ref in ENV_REF.findall(value):
                check(
                    definition(env, ref, position) is not None,
                    f"container {name}'s {var} reads $({ref}), which no variable before it "
                    "defines, so the kubelet leaves it verbatim",
                )
            url = urllib.parse.urlsplit(value)
            credentials = [("the URL's password", url.password)] if url.password else []
            credentials += [
                (f"query parameter {key}", item)
                for key, item in urllib.parse.parse_qsl(url.query)
                if SECRET_NAME.search(key)
            ]
            for where, item in credentials:
                ref = ENV_REF.fullmatch(item)
                check(
                    ref is not None and ref.group(1) in from_secrets,
                    f"container {name}'s {var} carries {where} as {item!r}, not as $(VAR) of a "
                    "variable read from a Secret",
                )


def check_shared_env(deployment):
    for (first, second), names in SHARED_ENV.items():
        for name in names:
            mine = definition(env_of(named(deployment, first)), name)
            theirs = definition(env_of(named(deployment, second)), name)
            check(
                mine is not None and mine == theirs,
                f"containers {first} and {second} define {name} differently: {mine} and {theirs}",
            )


def check_secret_refs(docs, deployment, stage):
    providers = secret_providers(docs)
    for secret, keys in providers.items():
        check(
            keys is not None,
            f"the Secret {secret} carries keys a render cannot know: a whole leaf, or a generator "
            "the chart does not render",
        )
    for container in pod_containers(deployment):
        for entry in container.get("env") or []:
            ref = secret_ref(entry)
            if ref is None:
                continue
            check(
                ref["key"] in (providers.get(ref["name"]) or ()),
                f"container {container['name']}'s {entry['name']} reads key {ref['key']} of the "
                f"Secret {ref['name']}, which nothing in {stage.namespace} provides; the pod "
                "cannot start",
            )
        for source in container.get("envFrom") or []:
            secret = (source.get("secretRef") or {}).get("name")
            check(
                secret is None or secret in providers,
                f"container {container['name']} takes its env from the Secret {secret}, which "
                f"nothing in {stage.namespace} provides",
            )


def check_external_secrets(docs, deployment, stage):
    secrets = [doc for doc in docs if doc["kind"] == "ExternalSecret"]
    for doc in secrets:
        if "secretStoreRef" in doc["spec"]:
            check(
                doc["spec"]["secretStoreRef"] == STORE,
                f"{label(doc)} reads through {doc['spec']['secretStoreRef']}, not {STORE}",
            )

    env = env_of(named(deployment, "backend"))
    for var, (secret, leaf, prop) in stage.leaves.items():
        ref = secret_ref(env.get(var, (None, {}))[1]) or {}
        check(
            ref.get("name") == secret,
            f"the backend's {var} reads the Secret {ref.get('name')}, not {secret}",
        )
        filled = [
            doc for doc in secrets
            if (doc["spec"].get("target") or {}).get("name") == ref.get("name")
            and "secretStoreRef" in doc["spec"]
        ]
        sources = [
            item["remoteRef"] for doc in filled for item in doc["spec"].get("data") or []
            if item["secretKey"] == ref.get("key")
        ]
        check(
            sources == [{"key": leaf, "property": prop}],
            f"the backend's {var} reads {ref}, which no ExternalSecret fills from {leaf} "
            f"property {prop}: {sources}",
        )

    generators = sorted(doc["metadata"]["name"] for doc in docs if doc["kind"] == "Password")
    check(
        generators == sorted(GENERATED),
        f"the chart generates the secrets {generators}, not {sorted(GENERATED)}",
    )
    for name in sorted(GENERATED):
        generator = find(docs, "Password", name)
        secret = find(docs, "ExternalSecret", name)
        check(
            generator is not None and secret is not None,
            f"no Password generator and ExternalSecret {name}, the in-cluster generated secret",
        )
        if generator is None or secret is None:
            continue
        spec = secret["spec"]
        check(
            spec.get("refreshInterval") == "0"
            and (spec.get("target") or {}).get("name") == name
            and spec.get("dataFrom") == [{"sourceRef": {"generatorRef": {
                "apiVersion": generator["apiVersion"], "kind": "Password", "name": name,
            }}}],
            f"ExternalSecret/{name} does not generate its Secret once from Password/{name}",
        )
        for doc in (generator, secret):
            check(
                (doc["metadata"].get("annotations") or {}).get("argocd.argoproj.io/sync-wave")
                == "-1",
                f"{label(doc)} is not at wave -1, ahead of the Deployment that reads it",
            )


def check_no_literal_secrets(docs, text):
    check(
        not [doc for doc in docs if doc["kind"] == "Secret"],
        "renders a Secret: its value would be in the commit; ESO or Terraform makes every Secret",
    )
    bearer = LITERAL_BEARER.search(text)
    check(
        bearer is None,
        f"the render carries a literal bearer token: {bearer and bearer.group(0)}",
    )


def check_site_service(docs, deployment, stage):
    fronted = [
        doc for doc in docs if doc["kind"] == "Service"
        and any(key.startswith(FRONT_DOOR) for key in doc["metadata"].get("annotations") or {})
    ]
    check(
        [label(doc) for doc in fronted] == ["Service/pilot"],
        f"the front door's annotations are on {[label(doc) for doc in fronted]}, not on the one "
        "Service/pilot",
    )
    service = find(docs, "Service", "pilot")
    if service is None:
        return
    annotations = {
        key: value for key, value in (service["metadata"].get("annotations") or {}).items()
        if key.startswith(FRONT_DOOR)
    }
    check(
        annotations == stage.annotations,
        f"Service/pilot's front-door annotations are {annotations}, not {stage.annotations}",
    )
    labels = deployment["spec"]["template"]["metadata"].get("labels") or {}
    selector = service["spec"].get("selector") or {}
    check(
        selector and selector.items() <= labels.items(),
        f"Service/pilot selects {selector}, which the pod's labels {labels} do not match",
    )
    ui = named(deployment, "ui")
    targets = [port.get("targetPort") for port in service["spec"]["ports"]]
    check(
        targets and all(container_port(ui, target) == UI_PORT for target in targets),
        f"Service/pilot targets {targets}, not the UI's nginx on {UI_PORT}",
    )


def check_render_stable(stage, first):
    # `now` renders to the second, so a render more than a second later differs if any value
    # reads the clock; one reading randomness differs on any second render.
    time.sleep(1.1)
    check(render(stage) == first, "two renders of the same commit differ")


def stage_markers(stage):
    """What names the stage alone: its host, its KubeCoder, its leaves."""
    return {stage.host, stage.kubecoder, *(leaf for _, leaf, _ in stage.leaves.values())}


def check_isolation(renders):
    """No stage's values or render name another stage's host, KubeCoder or leaves: dev's sessions
    are never the operator's own fleet's, and production never reads dev's client."""
    for stage in STAGES:
        texts = {stage.values: values_text(stage), "its render": renders[stage.name]}
        for other in STAGES:
            if other is stage:
                continue
            for where, text in texts.items():
                named = sorted(marker for marker in stage_markers(other) if marker in text)
                check(not named, f"{stage.name}: {where} names {other.name}'s {named}")


def check_stage(stage):
    check_pin_slots(stage)
    first = render(stage)
    docs = objects(first)
    check_namespace_manifest(docs, stage)
    check_hook(docs, stage)
    check_scope(docs, stage)
    check_no_literal_secrets(docs, first)
    deployment = check_pod(docs, stage)
    check_no_gateway(docs)
    check_images(stage)
    check_links_unset(stage)
    if deployment is not None:
        check_env(deployment)
        check_shared_env(deployment)
        check_secret_refs(docs, deployment, stage)
        check_external_secrets(docs, deployment, stage)
        check_site_service(docs, deployment, stage)
        check_wiring(deployment)
        check_timing(deployment)
        check_controller(docs, deployment)
    check_render_stable(stage, first)
    return first, len(docs)


def main():
    check_library()
    check_chart_values()
    check_stages()
    renders = {}
    counts = {}
    for stage in STAGES:
        start = len(failures)
        renders[stage.name], counts[stage.name] = check_stage(stage)
        failures[start:] = [f"{stage.name}: {failure}" for failure in failures[start:]]
    check_isolation(renders)

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}", file=sys.stderr)
        sys.exit(1)
    rendered = ", ".join(f"{name} ({count} objects)" for name, count in counts.items())
    print(f"ok: {rendered} render as Argo renders them")


main()
