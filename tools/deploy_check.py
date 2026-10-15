#!/usr/bin/env python3
"""Check the deployment artefacts in ``deploy/`` and the CI workflows — statically, and with reasons.

There is no container runtime and no cluster in the environment this repository is built in
(``which docker podman buildah`` finds nothing; there is no ``kubectl``). That is a fact about the
build environment, not about the artefacts, and the two ways of handling it are both bad: shipping
unchecked manifests and hoping, or shipping none and calling the deployment story "out of scope".

This tool takes the third way. It cannot run a container, so it does not claim to; what it *can* do
is read every document, render the Helm chart with its declared values, and assert the invariants
that this project actually cares about — the ones whose violation is a defect regardless of which
cluster it lands on:

* every document parses, and the chart renders under its own declared defaults;
* every ``.Values`` reference is declared, and every declared value is used (an interface that
  drifts from its implementation is worse than no interface);
* the pod is non-root, cannot escalate, drops all capabilities, and runs a read-only root
  filesystem with an explicit writable ``/tmp`` and evidence volume;
* every container that can be probed declares the port the probe uses — a probe on a stale port is
  a Deployment that never becomes Ready;
* requests and limits exist, images are pinned (no ``:latest``), and there is no ``hostPath``, no
  host namespace, and no ``privileged: true``;
* the policy embedded in each ConfigMap is parsed by the *project's own* policy loader, so a
  manifest cannot ship a policy that Docket would refuse to read;
* the compose file's verifier waits for the run to finish and mounts evidence read-only;
* the CI workflows parse and have jobs with steps (a workflow with a YAML error runs nothing,
  which looks like a green build from the outside);
* every path referenced from these artefacts exists.

The load-bearing test of this tool is not that it passes on this repository — it is that it **fails
on a broken artefact**. ``tests/test_deploy_check.py`` breaks each invariant on purpose, one
fixture per check, and asserts the tool notices; a checker that cannot fail is decoration.

Exit codes: ``0`` all checks passed, ``1`` at least one failed, ``2`` the check could not run
(missing dependency, unreadable tree). A check never reports success for work it did not do — an
absent PyYAML is a ``2`` with the reason on stderr, never a silent pass, and
``tests/test_deploy_check.py`` skips with that same reason instead of failing, so a bare clone's
suite is green-with-skips rather than red.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The repository this tool lives in. `--root` re-points the checker at a copy, which is how its
#: own tests prove it fails on a broken artefact: copy the tree, break one thing, assert the check
#: notices. The policy loader is always imported from *this* checkout, because a copied tree in a
#: test is not a second Docket installation.
HERE = Path(__file__).resolve().parents[1]
ROOT = HERE
DEPLOY = ROOT / "deploy"


def set_root(path: Path | str) -> None:
    """Point the checks at another tree (used by `--root` and by the tests)."""
    global ROOT, DEPLOY  # noqa: PLW0603 - a module-level option, set once per process
    ROOT = Path(path).resolve()
    DEPLOY = ROOT / "deploy"


#: The Helm constructs the renderer implements. Anything outside this set is a *failure*, not a
#: skipped file: a checker that quietly ignores the template it does not understand reports a green
#: tick for an artefact it never looked at.
SUPPORTED_TEMPLATE_DOC = (
    "{{ .Values.a.b }}, {{ .Values.a.b | quote }}, {{ . }} and {{ .field }} inside a range, "
    "{{ if .Values.x }}/{{ if not .Values.x }}, {{ else }}, {{ range .Values.list }}, {{ end }}, "
    "and the {{- -}} trim markers"
)

VALUES_REF = re.compile(r"\.Values\.([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)")
#: A bare substitution: a `.Values` path, `.` or a field of the ranged item, optionally quoted.
EXPRESSION_OK = re.compile(r"^\.(?:Values\.)?[A-Za-z_][A-Za-z0-9_.]*$|^\.$")
TOKEN = re.compile(r"\{\{(-?)(.*?)(-?)\}\}", re.S)

LATEST_TAGS = {"latest", ""}


class DeployCheckError(Exception):
    """A malformed artefact, or a template construct this tool refuses to guess at."""


# --------------------------------------------------------------------------- #
# A declared subset of the Helm template language
# --------------------------------------------------------------------------- #


@dataclass
class Text:
    value: str


@dataclass
class Expr:
    value: str
    where: str


@dataclass
class If:
    condition: str
    body: list[Any]
    otherwise: list[Any] = field(default_factory=list)
    where: str = ""


@dataclass
class Range:
    source: str
    body: list[Any]
    where: str = ""


def _tokenise(text: str, where: str) -> list[tuple[str, str, bool, bool]]:
    """``(kind, payload, trim_before, trim_after)`` for each literal or directive."""
    pieces: list[tuple[str, str, bool, bool]] = []
    position = 0
    for match in TOKEN.finditer(text):
        literal = text[position : match.start()]
        trim_before = bool(match.group(1))
        trim_after = bool(match.group(3))
        if literal:
            pieces.append(("text", literal, False, trim_before))
        pieces.append(("directive", match.group(2).strip(), trim_before, trim_after))
        position = match.end()
    tail = text[position:]
    if tail:
        pieces.append(("text", tail, False, False))
    if not any(kind == "directive" for kind, *_ in pieces) and "{{" in text:
        raise DeployCheckError(f"{where}: unbalanced template action")
    return pieces


def _parse(pieces: list[tuple[str, str, bool, bool]], where: str, index: int = 0) -> tuple[list[Any], int]:
    """Recursive-descent parse of the token stream into nodes. Returns ``(nodes, next_index)``."""
    nodes: list[Any] = []
    while index < len(pieces):
        kind, payload, _, trim_after = pieces[index]
        if kind == "text":
            value = payload.lstrip() if trim_after else payload
            if value:
                nodes.append(Text(value))
            index += 1
            continue
        if payload in {"end", "else"}:
            return nodes, index
        if payload.startswith("if "):
            condition = payload[3:].strip()
            body, index = _parse(pieces, where, index + 1)
            otherwise: list[Any] = []
            if index < len(pieces) and pieces[index][1] == "else":
                otherwise, index = _parse(pieces, where, index + 1)
            if index >= len(pieces) or pieces[index][1] != "end":
                raise DeployCheckError(f"{where}: an `if` block without `{{{{ end }}}}`")
            nodes.append(If(condition=condition, body=body, otherwise=otherwise, where=where))
            index += 1
            continue
        if payload.startswith("range "):
            source = payload[6:].strip()
            body, index = _parse(pieces, where, index + 1)
            if index >= len(pieces) or pieces[index][1] != "end":
                raise DeployCheckError(f"{where}: a `range` block without `{{{{ end }}}}`")
            nodes.append(Range(source=source, body=body, where=where))
            index += 1
            continue
        # A bare substitution, optionally piped: the only remaining form that is supported. The
        # pattern is checked explicitly rather than "anything that is not a keyword", so that
        # `{{ include "x" . }}` or `{{ default "a" .Values.b }}` is refused by name instead of
        # being substituted as a literal string and reported as a pass.
        head, _, pipe = payload.partition("|")
        if EXPRESSION_OK.match(head.strip()) and (not pipe or pipe.strip() == "quote"):
            nodes.append(Expr(value=payload, where=where))
            index += 1
            continue
        raise DeployCheckError(
            f"{where}: unsupported template construct {{{{ {payload} }}}}; this checker implements "
            f"{SUPPORTED_TEMPLATE_DOC}"
        )
    return nodes, index


def _lookup(values: dict[str, Any], dotted: str, where: str) -> Any:
    node: Any = values
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise DeployCheckError(f"{where}: .Values.{dotted} is not declared in values.yaml")
        node = node[part]
    return node


def _resolve(expression: str, values: dict[str, Any], item: Any, where: str) -> Any:
    """Resolve a single expression: a `.Values` path, `.` , or `.field` inside a range."""
    expression = expression.strip()
    if expression == ".":
        return item
    if expression.startswith(".Values."):
        return _lookup(values, expression[len(".Values.") :], where)
    if expression.startswith(".") and item is not None:
        key = expression[1:]
        if isinstance(item, dict) and key in item:
            return item[key]
        raise DeployCheckError(f"{where}: {expression!r} is not a field of the ranged item")
    raise DeployCheckError(
        f"{where}: unsupported expression {expression!r}; this checker implements {SUPPORTED_TEMPLATE_DOC}"
    )


def _evaluate(expression: str, values: dict[str, Any], item: Any, where: str) -> Any:
    parts = [part.strip() for part in expression.split("|")]
    value = _resolve(parts[0], values, item, where)
    for pipe in parts[1:]:
        if pipe == "quote":
            if not isinstance(value, str):
                raise DeployCheckError(f"{where}: `| quote` applied to a non-string value {value!r}")
            value = f'"{value}"'
        else:
            raise DeployCheckError(
                f"{where}: unsupported pipe {pipe!r}; this checker implements {SUPPORTED_TEMPLATE_DOC}"
            )
    return value


def _render_nodes(nodes: list[Any], values: dict[str, Any], item: Any, where: str) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, Text):
            out.append(node.value)
        elif isinstance(node, Expr):
            value = _evaluate(node.value, values, item, node.where)
            if not isinstance(value, str | int | float | bool):
                raise DeployCheckError(f"{where}: {node.value!r} does not render as a scalar")
            out.append(str(value))
        elif isinstance(node, If):
            condition = node.condition
            negate = condition.startswith("not ")
            if negate:
                condition = condition[4:].strip()
            value = _evaluate(condition, values, item, node.where)
            truthy = bool(value) is not negate
            out.append(_render_nodes(node.body if truthy else node.otherwise, values, item, where))
        elif isinstance(node, Range):
            source = _evaluate(node.source, values, item, node.where)
            if not isinstance(source, list):
                raise DeployCheckError(f"{where}: `range` needs a list, got {type(source).__name__}")
            for entry in source:
                out.append(_render_nodes(node.body, values, entry, where))
    return "".join(out)


def render(template: str, values: dict[str, Any], where: str) -> str:
    """Render ``template`` with ``values`` under the declared subset."""
    pieces = _tokenise(template, where)
    nodes, index = _parse(pieces, where)
    if index != len(pieces):  # pragma: no cover - _parse consumes end/else only via blocks
        raise DeployCheckError(f"{where}: unexpected {pieces[index][1]!r}")
    return _render_nodes(nodes, values, None, where)


def render_chart(chart: Path, values: dict[str, Any] | None = None) -> dict[str, str]:
    """Render every template in ``chart`` with ``values`` (default: the chart's own values.yaml)."""
    if values is None:
        values = load_yaml(chart / "values.yaml")
    rendered = {}
    for template in sorted((chart / "templates").glob("*")):
        if template.name.endswith("/"):
            continue
        rendered[template.name] = render(template.read_text(encoding="utf-8"), values, str(template))
    if not rendered:
        raise DeployCheckError(f"{chart}/templates is empty")
    return rendered


def load_yaml(path: Path) -> Any:
    yaml = _yaml(where=None)
    with path.open("rb") as handle:
        return yaml.safe_load(handle)


def _yaml(where: str | None):
    try:
        import yaml  # noqa: PLC0415 - the dependency is optional and the failure is reported
    except ImportError as exc:  # pragma: no cover - covered in a subprocess: see the test named
        # `test_a_missing_pyyaml_is_exit_2_with_a_reason_never_a_pass`, which shadows this import
        # rather than uninstalling the package.
        # `raise SystemExit("text")` prints the text and exits **1** — the code this tool uses for
        # "a check failed". That would make "could not run" indistinguishable from "found a
        # defect", which is the one distinction an operator script has to get right, so the code is
        # set explicitly and the reason is written to stderr.
        print(
            "deploy_check: PyYAML is not installed, so the deployment artefacts cannot be read.\n"
            + (f"(while reading {where})\n" if where else "")
            + "This is not a pass: install it (`pip install pyyaml`, or `pip install -e '.[deploy]'`) "
            "and run this again.\n"
            "Neither this script nor `make deploy-check` installs it for you, deliberately: a check "
            "that fetches its own dependencies is a check whose result depends on what it fetched.\n"
            f"({exc})",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return yaml


#: Templates that are Kubernetes documents rather than prose (NOTES.txt is rendered and checked for
#: substitution errors like any other template, but it is not YAML and is not parsed as such).
YAML_SUFFIXES = {".yaml", ".yml"}


def documents(text: str, where: str) -> list[dict[str, Any]]:
    """Every non-empty YAML document in ``text``, each required to be a mapping with a kind."""
    yaml = _yaml(where)
    loaded: list[dict[str, Any]] = []
    try:
        raw = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise DeployCheckError(f"{where}: not valid YAML: {exc}") from exc
    for index, document in enumerate(raw):
        if document is None:
            continue
        if not isinstance(document, dict):
            raise DeployCheckError(f"{where}: document {index} is a {type(document).__name__}, not a mapping")
        loaded.append(document)
    return loaded


# --------------------------------------------------------------------------- #
# The checks
# --------------------------------------------------------------------------- #


@dataclass
class Result:
    name: str
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


CHECKS: list[Any] = []


def check(name: str):
    def wrapper(function):
        CHECKS.append((name, function))
        return function

    return wrapper


def _containers(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from document.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []


def _images(document: dict[str, Any]) -> Iterator[str]:
    for container in _containers(document):
        image = container.get("image")
        if isinstance(image, str):
            yield image


def _pods() -> Iterator[tuple[str, dict[str, Any]]]:
    """Every Deployment/StatefulSet/DaemonSet rendered from the chart or written by hand."""
    for path, text in _all_documents():
        for document in documents(text, path):
            if document.get("kind") in {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}:
                yield path, document


def _kubernetes_documents() -> Iterator[tuple[str, str]]:
    """The Kubernetes artefacts: `deploy/kubernetes/` and the chart's rendered YAML templates.

    `deploy/compose.yaml` is a Docker Compose file, not a Kubernetes document, and is checked as
    such; kustomization.yaml is a Kubernetes document with an apiVersion and a kind, so it is in.
    """
    for path, text in _all_documents():
        if "kubernetes" in path or path.endswith(".yaml") and "templates" in path:
            yield path, text


def _all_documents() -> Iterator[tuple[str, str]]:
    """Every YAML artefact in deploy/, chart templates rendered."""
    chart = DEPLOY / "helm" / "docket"
    for name, text in render_chart(chart).items():
        if Path(name).suffix in YAML_SUFFIXES:
            yield f"{chart}/templates/{name}", text
    for path in sorted(DEPLOY.rglob("*.yaml")):
        if "helm" in path.parts:
            continue
        yield str(path), path.read_text(encoding="utf-8")


@check("chart.metadata")
def chart_metadata() -> list[str]:
    chart = DEPLOY / "helm" / "docket"
    problems: list[str] = []
    metadata = load_yaml(chart / "Chart.yaml")
    if metadata.get("apiVersion") != "v2":
        problems.append(f"Chart.yaml apiVersion is {metadata.get('apiVersion')!r}, not 'v2'")
    for key in ("name", "description", "version", "appVersion", "home", "sources"):
        if not metadata.get(key):
            problems.append(f"Chart.yaml is missing {key!r}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(metadata.get("version", ""))):
        problems.append(f"Chart.yaml version {metadata.get('version')!r} is not x.y.z")
    if not (chart / "values.yaml").exists() or not (chart / "templates").is_dir():
        problems.append("the chart needs values.yaml and templates/")
    return problems


@check("chart.values-are-an-interface")
def chart_values_interface() -> list[str]:
    """Every value a template uses is declared; every declared value is used."""
    chart = DEPLOY / "helm" / "docket"
    values = load_yaml(chart / "values.yaml")

    declared: set[str] = set()

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(f"{prefix}.{key}" if prefix else str(key), value)
        else:
            declared.add(prefix)

    walk("", values)

    used: set[str] = set()
    for template in sorted((chart / "templates").glob("*")):
        text = template.read_text(encoding="utf-8")
        for match in VALUES_REF.finditer(text):
            used.add(match.group(1))
        # a range over a list still counts as using its items' parent path
        for match in re.finditer(r"range\s+\.Values\.([A-Za-z0-9_.]+)", text):
            used.add(match.group(1))

    problems = [f"template uses undeclared .Values.{name}" for name in sorted(used - declared)]
    problems += [f"values.yaml declares .Values.{name}, which no template uses" for name in sorted(declared - used)]
    return problems


@check("chart.renders")
def chart_renders() -> list[str]:
    """The chart renders under its own values, and every rendered document is valid YAML."""
    problems: list[str] = []
    for name, text in render_chart(DEPLOY / "helm" / "docket").items():
        if not text.strip():
            problems.append(f"{name} renders to nothing under the chart's own values")
            continue
        if Path(name).suffix not in YAML_SUFFIXES:
            continue
        try:
            documents(text, name)
        except DeployCheckError as exc:
            problems.append(str(exc))
    return problems


@check("documents.are-declarations")
def documents_are_declarations() -> list[str]:
    problems: list[str] = []
    seen = 0
    for path, text in _kubernetes_documents():
        for document in documents(text, path):
            seen += 1
            if not document.get("apiVersion") or not document.get("kind"):
                problems.append(f"{path}: a document without apiVersion/kind")
    if seen == 0:
        problems.append("no Kubernetes documents found under deploy/")
    return problems


@check("images.are-pinned")
def images_are_pinned() -> list[str]:
    problems: list[str] = []
    for path, document in _pods():
        for image in _images(document):
            tag = image.rsplit("/", 1)[-1].split(":", 1)
            if len(tag) == 1 or tag[1] in LATEST_TAGS:
                problems.append(f"{path}: image {image!r} has no pinned tag")
    return problems


@check("pod.is-hardened")
def pod_is_hardened() -> list[str]:
    problems: list[str] = []
    for path, document in _pods():
        spec = document.get("spec", {}).get("template", {}).get("spec", {})
        context = spec.get("securityContext") or {}
        if context.get("runAsNonRoot") is not True:
            problems.append(f"{path}: pod securityContext.runAsNonRoot is not true")
        if not isinstance(context.get("runAsUser"), int) or context.get("runAsUser") in (0,):
            problems.append(f"{path}: pod securityContext.runAsUser is not a non-zero integer")
        if not context.get("seccompProfile"):
            problems.append(f"{path}: pod securityContext has no seccompProfile")
        for key in ("hostNetwork", "hostPID", "hostIPC"):
            if spec.get(key):
                problems.append(f"{path}: {key} is enabled")
        for container in _containers(document):
            name = container.get("name", "?")
            security = container.get("securityContext") or {}
            if security.get("allowPrivilegeEscalation") is not False:
                problems.append(f"{path}: container {name} does not set allowPrivilegeEscalation: false")
            if security.get("readOnlyRootFilesystem") is not True:
                problems.append(f"{path}: container {name} does not set readOnlyRootFilesystem: true")
            if security.get("privileged"):
                problems.append(f"{path}: container {name} is privileged")
            caps = (security.get("capabilities") or {}).get("drop") or []
            if "ALL" not in caps:
                problems.append(f"{path}: container {name} does not drop ALL capabilities")
        for volume in spec.get("volumes") or []:
            if "hostPath" in volume:
                problems.append(f"{path}: volume {volume.get('name')} uses hostPath")
    return problems


@check("containers.have-resources")
def containers_have_resources() -> list[str]:
    problems: list[str] = []
    for path, document in _pods():
        for container in _containers(document):
            name = container.get("name", "?")
            resources = container.get("resources") or {}
            for section in ("requests", "limits"):
                values = resources.get(section) or {}
                for key in ("cpu", "memory"):
                    if not values.get(key):
                        problems.append(f"{path}: container {name} has no {section}.{key}")
    return problems


@check("probes.use-a-declared-port")
def probes_use_a_declared_port() -> list[str]:
    """A probe on a port the container does not declare is a pod that never becomes Ready."""
    problems: list[str] = []
    pattern = re.compile(r"127\.0\.0\.1:(\d+)")
    for path, document in _pods():
        for container in _containers(document):
            name = container.get("name", "?")
            declared = {str(port.get("containerPort")) for port in container.get("ports") or []}
            probes = [
                (kind, container[kind])
                for kind in ("livenessProbe", "readinessProbe", "startupProbe")
                if container.get(kind)
            ]
            if not probes and container.get("ports"):
                # A container with no declared port may be a job that runs to completion; one that
                # publishes a port is a server, and a server nobody probes is a server whose death
                # is discovered by the next request.
                problems.append(f"{path}: container {name} declares a port but no probes")
            for kind, probe in probes:
                commands = probe.get("exec", {}).get("command") or []
                for command in commands:
                    for port in pattern.findall(command):
                        if port not in declared:
                            problems.append(
                                f"{path}: container {name} {kind} probes 127.0.0.1:{port}, "
                                f"which is not a declared containerPort ({sorted(declared) or 'none'})"
                            )
                if not commands and not probe.get("httpGet") and not probe.get("tcpSocket"):
                    problems.append(f"{path}: container {name} {kind} has no handler")
    return problems


@check("deployment.one-writer")
def deployment_one_writer() -> list[str]:
    """One writer per evidence directory: `Recreate`, and a PVC where the policy says it is."""
    problems: list[str] = []
    for path, document in _pods():
        if document.get("kind") != "Deployment":
            continue
        strategy = document.get("spec", {}).get("strategy", {}).get("type")
        if strategy != "Recreate":
            problems.append(f"{path}: strategy.type is {strategy!r}; two pods would share one evidence volume")
        spec = document.get("spec", {}).get("template", {}).get("spec", {})
        mounts = {
            mount.get("mountPath")
            for container in _containers(document)
            for mount in container.get("volumeMounts") or []
        }
        if "/work/.docket" not in mounts:
            problems.append(f"{path}: nothing is mounted at /work/.docket, where the policy points --docket-dir")
        names = {volume.get("name") for volume in spec.get("volumes") or []}
        for container in _containers(document):
            for mount in container.get("volumeMounts") or []:
                if mount.get("name") not in names:
                    problems.append(f"{path}: mount {mount.get('name')!r} has no matching volume")
    return problems


@check("configmap.policy-parses")
def configmap_policy_parses() -> list[str]:
    """Every policy embedded in a ConfigMap is read by Docket's own policy loader."""
    sys.path.insert(0, str(HERE / "src"))
    from docket.policy import Policy  # noqa: PLC0415 - imported here so the failure is legible

    problems: list[str] = []
    seen = 0
    for path, text in _all_documents():
        for document in documents(text, path):
            if document.get("kind") != "ConfigMap":
                continue
            for key, body in (document.get("data") or {}).items():
                if not str(key).endswith(".toml"):
                    continue
                seen += 1
                try:
                    parsed = tomllib.loads(body)
                except tomllib.TOMLDecodeError as exc:
                    problems.append(f"{path}: {key} is not valid TOML: {exc}")
                    continue
                loader = getattr(Policy, "from_dict", None)
                if loader is None:
                    if "caps" not in parsed:
                        problems.append(f"{path}: {key} has no [caps] section")
                    continue
                try:
                    loader(parsed)
                except Exception as exc:  # noqa: BLE001 - whatever the loader says is the answer
                    problems.append(f"{path}: {key} is not a policy Docket accepts: {exc}")
    if seen == 0:
        problems.append("no policy TOML found in any ConfigMap: the manifests do not configure a run")
    return problems


@check("networkpolicy.default-deny")
def networkpolicy_default_deny() -> list[str]:
    problems: list[str] = []
    seen = 0
    for path, text in _all_documents():
        for document in documents(text, path):
            if document.get("kind") != "NetworkPolicy":
                continue
            seen += 1
            spec = document.get("spec") or {}
            types = spec.get("policyTypes") or []
            if "Ingress" not in types or "Egress" not in types:
                problems.append(f"{path}: policyTypes must include Ingress and Egress, got {types}")
            if spec.get("ingress") != []:
                problems.append(f"{path}: ingress is not empty; nothing should reach a live run")
            egress = spec.get("egress") or []
            if not egress:
                problems.append(f"{path}: no egress rules: the pod could not resolve DNS or reach a provider")
            if not spec.get("podSelector"):
                problems.append(f"{path}: no podSelector, so the policy applies to nothing")
    if seen == 0:
        problems.append("no NetworkPolicy in the manifests")
    return problems


@check("compose.waits-and-is-read-only")
def compose_waits_and_is_read_only() -> list[str]:
    problems: list[str] = []
    compose = load_yaml(DEPLOY / "compose.yaml")
    services = compose.get("services") or {}
    for required in ("provider", "boundary", "verify"):
        if required not in services:
            problems.append(f"compose.yaml has no {required!r} service")
    verify = services.get("verify") or {}
    conditions = [(dependency or {}).get("condition") for dependency in (verify.get("depends_on") or {}).values()]
    if "service_completed_successfully" not in conditions:
        problems.append("the verifier does not wait for the run to finish before verifying")
    for name, service in services.items():
        if service.get("ports"):
            problems.append(
                f"service {name} publishes ports; a boundary and a scripted provider stay on the compose network"
            )
        if service.get("read_only") is not True:
            problems.append(f"service {name} does not set read_only: true")
        caps = service.get("cap_drop") or []
        if "ALL" not in caps:
            problems.append(f"service {name} does not drop ALL capabilities")
        for mount in service.get("volumes") or []:
            if isinstance(mount, str) and "/etc/docket" in mount and not mount.endswith(":ro"):
                problems.append(f"service {name} mounts the policy read-write: {mount}")
        if name != "verify" and not service.get("healthcheck"):
            problems.append(f"service {name} has no healthcheck")
    evidence_mounts = [
        mount for mount in (verify.get("volumes") or []) if isinstance(mount, str) and "/work/.docket" in mount
    ]
    if not evidence_mounts or not all(mount.endswith(":ro") for mount in evidence_mounts):
        problems.append("the verifier mounts the evidence volume writable")
    return problems


@check("kustomization.paths-exist")
def kustomization_paths_exist() -> list[str]:
    problems: list[str] = []
    path = DEPLOY / "kubernetes" / "kustomization.yaml"
    if not path.exists():
        return [f"{path} does not exist"]
    resource_list = load_yaml(path).get("resources") or []
    if not resource_list:
        problems.append("kustomization.yaml lists no resources")
    for entry in resource_list:
        target = path.parent / entry
        if not target.exists():
            problems.append(f"kustomization.yaml references {entry}, which does not exist")
            continue
        if not documents(target.read_text(encoding="utf-8"), str(target)):
            problems.append(f"kustomization.yaml references {entry}, which holds no documents")
    return problems


@check("dockerfile.is-buildable-by-inspection")
def dockerfile_is_buildable_by_inspection() -> list[str]:
    """The Dockerfile's copies exist, it installs as a non-root user, and it hides no surprises."""
    problems: list[str] = []
    path = DEPLOY / "Dockerfile"
    if not path.exists():
        return [f"{path} does not exist"]
    text = path.read_text(encoding="utf-8")
    for match in re.finditer(r"^\s*COPY\s+(?:--\S+\s+)*(.+)$", text, re.M):
        sources = match.group(1).split()
        if len(sources) < 2:
            problems.append(f"Dockerfile: COPY {match.group(1)!r} has no destination")
            continue
        from_stage = "--from=" in match.group(0)
        for source in sources[:-1]:
            if from_stage or source in {".", "./"} or source.startswith(("http", "/")):
                # `COPY --from=build /install /usr/local` copies from another stage: not a path in
                # this repository, and checking it would be checking the wrong thing.
                continue
            if not (ROOT / source).exists():
                problems.append(f"Dockerfile copies {source!r}, which is not in the repository")
    if re.search(r"^\s*ADD\s", text, re.M):
        problems.append("Dockerfile uses ADD; COPY is the directive that does not unpack or fetch")
    if re.search(r"curl[^\n]*\|\s*(?:ba)?sh", text):
        problems.append("Dockerfile pipes a download into a shell")
    users = re.findall(r"^\s*USER\s+(\S+)", text, re.M)
    if not users:
        problems.append("Dockerfile never sets USER: the container would run as root")
    definite = [name for name in users if name in {"root", "0", "0:0", "root:root"}]
    if definite or not any(name not in {"root", "0"} for name in users):
        problems.append(f"Dockerfile runs as {users[-1]!r}; a supervised agent needs no root")
    if "STOPSIGNAL" not in text:
        problems.append("Dockerfile sets no STOPSIGNAL; SIGTERM is what lets a run close its receipt")
    if re.search(r"^\s*ENV\s+\S*(?:KEY|TOKEN|SECRET|PASSWORD)\S*=", text, re.M | re.I):
        problems.append("Dockerfile bakes a credential-looking variable into the image")
    if not (ROOT / ".dockerignore").exists():
        problems.append("no .dockerignore: the build context would carry .git, ledgers and dist/")
    return problems


@check("workflows.parse")
def workflows_parse() -> list[str]:
    """The CI workflows are deployment artefacts too: they are what runs the checks.

    A YAML file that does not parse is a workflow that silently does not exist — GitHub reports a
    syntax error and runs nothing, which from the outside looks exactly like a green build. This
    caught a real defect in this repository's own CI while the deploy job was being added: an
    unquoted step name containing a colon ("deploy artefacts (static: no container ...)").
    """
    problems: list[str] = []
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
    if not workflows:
        return ["no CI workflows found"]
    for path in workflows:
        try:
            document = load_yaml(path)
        except Exception as exc:  # noqa: BLE001 - any parse failure is the finding
            problems.append(f"{path}: does not parse as YAML: {exc}")
            continue
        if not isinstance(document, dict):
            problems.append(f"{path}: not a mapping")
            continue
        jobs = document.get("jobs")
        if not isinstance(jobs, dict) or not jobs:
            problems.append(f"{path}: no jobs")
            continue
        for name, job in jobs.items():
            if not isinstance(job, dict):
                problems.append(f"{path}: job {name!r} is not a mapping")
                continue
            if job.get("runs-on") in (None, ""):
                problems.append(f"{path}: job {name!r} has no runs-on")
            steps = job.get("steps")
            if not isinstance(steps, list) or not steps:
                problems.append(f"{path}: job {name!r} has no steps")
                continue
            for index, step in enumerate(steps):
                if not isinstance(step, dict) or not (step.get("uses") or step.get("run")):
                    problems.append(f"{path}: job {name!r} step {index} neither uses an action nor runs a command")
    return problems


@check("references.resolve")
def references_resolve() -> list[str]:
    """Every path referenced from an artefact in deploy/ exists."""
    problems: list[str] = []
    # A backtick is the usual way these paths are written in prose (`docs/testing.md`), so it counts
    # as a boundary too — a checker that only noticed unquoted paths would miss most of them.
    pattern = re.compile(
        r"(?:^|[\s(`])((?:deploy|docs|src|spec|tests|tools)/[A-Za-z0-9_./-]+\.(?:md|py|yaml|toml|json))"
    )
    for path in sorted(DEPLOY.rglob("*")):
        if not path.is_file() or path.suffix not in {".yaml", ".yml", ".md", ".json"}:
            continue
        for reference in pattern.findall(path.read_text(encoding="utf-8")):
            if not (ROOT / reference).exists():
                problems.append(f"{path}: references {reference}, which does not exist")
    return problems


# --------------------------------------------------------------------------- #
# Running them
# --------------------------------------------------------------------------- #


def run(selected: list[str] | None = None) -> tuple[list[Result], list[str]]:
    results: list[Result] = []
    for name, function in CHECKS:
        if selected and not any(name.startswith(prefix) for prefix in selected):
            continue
        result = Result(name=name)
        try:
            result.problems = list(function() or [])
        except DeployCheckError as exc:
            result.problems = [str(exc)]
        except Exception as exc:  # noqa: BLE001 - a crash in a check is a failed check, not a traceback
            result.problems = [f"{type(exc).__name__}: {exc}"]
        results.append(result)
    # The render is reported by `chart.renders` when it fails, so it must not take the whole run
    # down with a traceback: `run()` returns what it has, and the failure is in the results.
    try:
        yaml_only = {
            name: text
            for name, text in render_chart(DEPLOY / "helm" / "docket").items()
            if Path(name).suffix in YAML_SUFFIXES
        }
    except DeployCheckError:
        yaml_only = {}
    manifest = "".join(f"# {name}\n{text}\n---\n" for name, text in sorted(yaml_only.items()))
    return results, [manifest]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tools/deploy_check.py",
        description="Check deploy/ statically: it cannot run a container, and it does not pretend to.",
    )
    parser.add_argument("--only", action="append", help="run only checks whose name starts with this")
    parser.add_argument("--root", type=Path, help="check this tree instead of the repository (for tests)")
    parser.add_argument("--json", action="store_true", help="machine-readable results")
    parser.add_argument("--print-rendered", action="store_true", help="print the rendered chart and stop")
    args = parser.parse_args(argv)
    if args.root:
        set_root(args.root)

    if args.print_rendered:
        rendered = render_chart(DEPLOY / "helm" / "docket")
        for name in sorted(rendered):
            sys.stdout.write(f"# ---- {name} ----\n{rendered[name]}\n")
        return 0

    results, rendered = run(args.only)
    failures = [result for result in results if not result.ok]
    if args.json:
        print(
            json.dumps(
                {
                    "checks": [{"name": r.name, "ok": r.ok, "problems": r.problems} for r in results],
                    "passed": len(results) - len(failures),
                    "failed": len(failures),
                    "rendered_documents": len(documents(rendered[0], "rendered chart")),
                },
                indent=2,
            )
        )
    else:
        for result in results:
            if result.ok:
                print(f"  PASS  {result.name}")
            else:
                print(f"  FAIL  {result.name}")
                for problem in result.problems:
                    print(f"          {problem}")
        print(
            f"\n{len(results) - len(failures)}/{len(results)} deploy checks passed "
            f"({len(documents(rendered[0], 'rendered chart'))} Kubernetes documents rendered and read)"
        )
        print(
            "What this does not do: run a container, apply a manifest, or reach a cluster. "
            "No container runtime or kubectl exists in the environment this ran in."
        )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
