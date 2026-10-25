#!/usr/bin/env python3
"""Check documentation links and the structural consistency of design contracts.

This is an offline documentation check, not a product integration test or a full
OpenAPI/JSON Schema conformance validator. Run from any working directory.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    import yaml
except ImportError:
    raise SystemExit(
        "PyYAML is required. Run: python3 -m pip install -r scripts/requirements-docs.txt"
    )


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "README.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "docs/01-requirements.md",
    "docs/02-architecture.md",
    "docs/03-data-and-authorization.md",
    "docs/04-interfaces.md",
    "docs/05-development-and-testing.md",
    "docs/06-deployment-and-operations.md",
    "docs/07-references.md",
    "docs/08-implementation-tickets.md",
    "specs/openapi.yaml",
    "specs/device-protocol.schema.json",
    "specs/run-events.schema.json",
    "specs/tool-contracts.schema.json",
]
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def without_fences(text: str) -> str:
    result: list[str] = []
    marker: str | None = None
    for line in text.splitlines():
        match = re.match(r"^\s*(`{3,}|~{3,})", line)
        if match:
            first = match.group(1)[0]
            if marker is None:
                marker = first
            elif first == marker:
                marker = None
            continue
        if marker is None:
            result.append(line)
    return "\n".join(result)


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print a JSON result.")
    args = parser.parse_args()
    errors: list[str] = []
    counts = {"markdown_files": 0, "local_links": 0, "schema_documents": 0,
              "local_schema_refs": 0, "http_operations": 0,
              "implementation_tickets": 0, "ticket_dependency_edges": 0,
              "ticket_requirement_coverage": 0}
    documents: dict[Path, dict] = {}

    for name in REQUIRED:
        path = ROOT / name
        if not path.is_file():
            errors.append(f"Missing required file: {name}")
        elif not path.read_bytes():
            errors.append(f"Empty required file: {name}")

    for path in sorted(ROOT.rglob("*.md")):
        counts["markdown_files"] += 1
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            errors.append(f"{path.relative_to(ROOT)}: invalid UTF-8: {exc}")
            continue
        if not re.search(r"^#\s+\S", text, re.M):
            errors.append(f"{path.relative_to(ROOT)}: missing title")
        if "/workspace/scratch/" in text:
            errors.append(f"{path.relative_to(ROOT)}: contains authoring workspace path")
        for target in re.findall(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)", without_fences(text)):
            target = target.strip().split(' "', 1)[0].strip("<>")
            url = urlsplit(target)
            if url.scheme or url.netloc or not url.path:
                continue
            destination = (path.parent / unquote(url.path)).resolve()
            counts["local_links"] += 1
            if not destination.is_relative_to(ROOT):
                errors.append(f"{path.relative_to(ROOT)}: link escapes package: {target}")
            elif not destination.exists():
                errors.append(f"{path.relative_to(ROOT)}: missing link target: {target}")

    # Verify the editable ticket document itself, without authoring-only JSON.
    ticket_path = ROOT / "docs/08-implementation-tickets.md"
    if ticket_path.is_file():
        ticket_text = ticket_path.read_text(encoding="utf-8")
        headings = list(re.finditer(r"^#{3,4}\s+(BA-[A-Z]+-\d{2})\s+(.+)$", ticket_text, re.M))
        ticket_ids = [match.group(1) for match in headings]
        known_ids = set(ticket_ids)
        counts["implementation_tickets"] = len(headings)
        if not headings:
            errors.append("Implementation ticket headings are missing")
        if len(known_ids) != len(ticket_ids):
            errors.append("Duplicate implementation ticket IDs")
        unknown = set(re.findall(r"BA-[A-Z]+-\d{2}", ticket_text)) - known_ids
        if unknown:
            errors.append(f"Undefined ticket references: {sorted(unknown)}")
        requirements = set(re.findall(r"FR-\d{3}", (ROOT / "docs/01-requirements.md").read_text()))
        known_tests = set(re.findall(r"T-\d{3}", (ROOT / "docs/05-development-and-testing.md").read_text()))
        unknown_tests = set(re.findall(r"T-\d{3}", ticket_text)) - known_tests
        if unknown_tests:
            errors.append(f"Undefined test references in tickets: {sorted(unknown_tests)}")
        covered: set[str] = set()
        dependencies: dict[str, list[str]] = {}
        for index, heading in enumerate(headings):
            ident = heading.group(1)
            end = headings[index + 1].start() if index + 1 < len(headings) else len(ticket_text)
            section = ticket_text[heading.end():end]
            row = re.search(r"^\| 依存PR \| (.*?) \|$", section, re.M)
            if not row:
                errors.append(f"{ident}: dependency row is missing")
            dependencies[ident] = re.findall(r"BA-[A-Z]+-\d{2}", row.group(1)) if row else []
            if len(dependencies[ident]) != len(set(dependencies[ident])):
                errors.append(f"{ident}: duplicate dependencies")
            counts["ticket_dependency_edges"] += len(dependencies[ident])
            row = re.search(r"^\| 関連要件 \| (.*?) \|$", section, re.M)
            related = set(re.findall(r"FR-\d{3}", row.group(1))) if row else set()
            if not related or related - requirements:
                errors.append(f"{ident}: missing or undefined requirement references")
            covered.update(related)
            acceptance = re.search(r"\*\*受入条件\*\*(.*?)\*\*検証方法\*\*", section, re.S)
            if not acceptance or not re.search(r"^- \[[ xX]\] \S", acceptance.group(1), re.M):
                errors.append(f"{ident}: acceptance checklist is missing")
        counts["ticket_requirement_coverage"] = len(covered)
        if requirements - covered:
            errors.append(f"Requirements without tickets: {sorted(requirements - covered)}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(ident: str) -> None:
            if ident in visiting:
                errors.append(f"Ticket dependency cycle at {ident}")
                return
            if ident in visited or ident not in dependencies:
                return
            visiting.add(ident)
            for dependency in dependencies[ident]:
                visit(dependency)
            visiting.remove(ident)
            visited.add(ident)

        for ident in ticket_ids:
            visit(ident)
        anchors = {
            re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
            for title in re.findall(r"^#{1,6}\s+(.+)$", ticket_text, re.M)
        }
        for fragment in re.findall(r"\[[^\]\n]+\]\(#([^\)\n]+)\)", ticket_text):
            if unquote(fragment) not in anchors:
                errors.append(f"Ticket heading link does not resolve: #{fragment}")

    def load(path: Path) -> dict:
        path = path.resolve()
        if path not in documents:
            if not path.is_relative_to(ROOT):
                raise ValueError("reference escapes package")
            text = path.read_text(encoding="utf-8")
            value = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
            if not isinstance(value, dict):
                raise ValueError("document root must be an object")
            documents[path] = value
        return documents[path]

    def resolve(reference: str, source: Path):
        url = urlsplit(reference)
        if url.scheme or url.netloc:
            return None
        target = (source.parent / unquote(url.path)).resolve() if url.path else source.resolve()
        value = load(target)
        if url.fragment:
            pointer = unquote(url.fragment)
            if not pointer.startswith("/"):
                raise ValueError(f"Only JSON Pointer local fragments are supported: {reference}")
            for token in pointer[1:].split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                value = value[int(token)] if isinstance(value, list) else value[token]
        return value

    schema_paths = sorted((ROOT / "specs").glob("*.json"))
    openapi_path = ROOT / "specs/openapi.yaml"
    all_paths = [openapi_path, *schema_paths]
    for path in all_paths:
        if not path.is_file():
            continue
        try:
            load(path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            errors.append(f"{path.relative_to(ROOT)}: parse error: {exc}")

    # A reference can load another local document, so drain a work list.
    checked: set[Path] = set()
    while set(documents) - checked:
        path = sorted(set(documents) - checked)[0]
        checked.add(path)
        value = documents[path]
        counts["schema_documents"] += 1
        for node in walk(value):
            reference = node.get("$ref")
            if not isinstance(reference, str):
                continue
            try:
                if not urlsplit(reference).scheme:
                    counts["local_schema_refs"] += 1
                resolve(reference, path)
            except (OSError, KeyError, IndexError, TypeError, ValueError, yaml.YAMLError) as exc:
                errors.append(f"{path.relative_to(ROOT)}: broken ref {reference}: {exc}")

    api = documents.get(openapi_path.resolve())
    if api:
        if not str(api.get("openapi", "")).startswith("3.1."):
            errors.append("OpenAPI contract must declare the selected 3.1 version")
        if not isinstance(api.get("info"), dict) or not api["info"].get("version"):
            errors.append("OpenAPI info.version is missing")
        operation_ids: set[str] = set()
        known_security = set(api.get("components", {}).get("securitySchemes", {}))
        for route, item in api.get("paths", {}).items():
            if not isinstance(item, dict):
                errors.append(f"Invalid path item: {route}")
                continue
            path_vars = set(re.findall(r"\{([^{}]+)\}", route))
            for method, operation in item.items():
                if method not in HTTP_METHODS:
                    continue
                counts["http_operations"] += 1
                if not isinstance(operation, dict):
                    errors.append(f"{method} {route}: operation is not an object")
                    continue
                operation_id = operation.get("operationId")
                if not operation_id or operation_id in operation_ids:
                    errors.append(f"{method} {route}: missing/duplicate operationId")
                operation_ids.add(operation_id)
                if not isinstance(operation.get("responses"), dict) or not operation["responses"]:
                    errors.append(f"{method} {route}: responses are missing")
                params = [*item.get("parameters", []), *operation.get("parameters", [])]
                actual: set[str] = set()
                for param in params:
                    try:
                        if "$ref" in param:
                            param = resolve(param["$ref"], openapi_path)
                        if param and param.get("in") == "path":
                            actual.add(param.get("name"))
                            if param.get("required") is not True:
                                errors.append(f"{method} {route}: path parameter must be required")
                    except (KeyError, TypeError, ValueError) as exc:
                        errors.append(f"{method} {route}: invalid parameter: {exc}")
                if actual != path_vars:
                    errors.append(f"{method} {route}: path parameters mismatch {actual} != {path_vars}")
                for requirement in operation.get("security", api.get("security", [])):
                    for name in requirement:
                        if name not in known_security:
                            errors.append(f"{method} {route}: unknown security scheme {name}")

    for path in schema_paths:
        schema = documents.get(path.resolve())
        if schema and schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            errors.append(f"{path.name}: expected JSON Schema draft 2020-12")

    result = {
        "status": "PASS" if not errors else "FAIL",
        "counts": counts,
        "errors": errors,
        "scope": "Offline document links, ticket IDs/dependencies/coverage, parsed contracts, local refs and OpenAPI structure only.",
        "excluded": [
            "Full OpenAPI or JSON Schema meta-schema conformance",
            "External URL availability and rendered Mermaid diagrams",
            "Implemented authorization, product behavior, cloud configuration or device performance",
        ],
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["status"])
        for key, value in counts.items():
            print(f"{key}: {value}")
        for error in errors:
            print(f"ERROR: {error}")
        print(result["scope"])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
