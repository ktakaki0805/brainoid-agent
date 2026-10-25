#!/usr/bin/env python3
"""Read-only ticket selection and progress validation; Python standard library only."""

from __future__ import annotations

import argparse
from datetime import datetime
import heapq
import json
from pathlib import Path
import re
import subprocess
import sys

TICKET_ID = r"BA-[A-Z]+-\d{2}"
STATUSES = {"todo", "in_progress", "in_review", "blocked", "done"}


def parse_tickets(text: str) -> dict[str, dict]:
    # Ignore examples inside Markdown fences.
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    text = "\n".join(lines)
    headings = list(re.finditer(r"^#{1,4}\s+(.+)$", text, re.M))
    tickets = {}
    for index, heading in enumerate(headings):
        title = heading.group(1)
        match = re.fullmatch(rf"({TICKET_ID})\s+(.+)", title)
        if not match:
            if title.startswith("BA-"):
                raise ValueError(f"Malformed ticket heading: {title}")
            continue
        ident, name = match.groups()
        if ident in tickets:
            raise ValueError(f"Duplicate ticket: {ident}")
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        section = text[heading.end():end]
        rows = re.findall(r"^\|\s*依存PR\s*\|\s*(.*?)\s*\|\s*$", section, re.M)
        if len(rows) != 1:
            raise ValueError(f"{ident}: expected exactly one dependency row")
        raw = re.sub(rf"\[({TICKET_ID})\]\(#[^\s)]+\)", r"\1", rows[0])
        deps = [] if raw == "なし" else re.findall(TICKET_ID, raw)
        if raw != "なし" and (not deps or re.sub(rf"{TICKET_ID}|[、,\s]", "", raw)):
            raise ValueError(f"{ident}: invalid dependency row: {rows[0]}")
        if len(deps) != len(set(deps)):
            raise ValueError(f"{ident}: duplicate dependency")
        tickets[ident] = {"title": name, "dependencies": deps}
    if not tickets:
        raise ValueError("No ticket detail headings found")
    for ident, ticket in tickets.items():
        unknown = set(ticket["dependencies"]) - tickets.keys()
        if unknown:
            raise ValueError(f"{ident}: undefined dependencies: {sorted(unknown)}")
    return tickets


def topological_order(tickets: dict[str, dict]) -> list[str]:
    rank = {ident: index for index, ident in enumerate(tickets)}
    indegree = {ident: len(t["dependencies"]) for ident, t in tickets.items()}
    children = {ident: [] for ident in tickets}
    for ident, ticket in tickets.items():
        for dep in ticket["dependencies"]:
            children[dep].append(ident)
    ready = [(rank[ident], ident) for ident, count in indegree.items() if count == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        _, ident = heapq.heappop(ready)
        order.append(ident)
        for child in children[ident]:
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(ready, (rank[child], child))
    if len(order) != len(tickets):
        remaining = [ident for ident, count in indegree.items() if count]
        raise ValueError(f"Dependency cycle (or dependent on a cycle): {remaining}")
    return order


def unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def validate_status(data: dict, tickets: dict[str, dict], root: Path) -> dict:
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Expected schema_version: 1")
    entries = data.get("tickets")
    if not isinstance(entries, dict) or entries.keys() != tickets.keys():
        actual = set(entries) if isinstance(entries, dict) else set()
        raise ValueError(f"Status IDs differ: missing={sorted(tickets.keys() - actual)}, unknown={sorted(actual - tickets.keys())}")
    for ident, entry in entries.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("status"), str) or entry["status"] not in STATUSES:
            raise ValueError(f"{ident}: invalid status")
        if entry["status"] != "todo":
            stamp = entry.get("updated_at", "")
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("timezone required")
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValueError(f"{ident}: updated_at needs an ISO 8601 timestamp with timezone") from exc
        blocker = entry.get("blocker")
        if entry["status"] == "blocked" and (not isinstance(blocker, str) or not blocker.strip()):
            raise ValueError(f"{ident}: blocked requires a blocker")
        if entry["status"] != "done":
            continue
        commit = entry.get("implementation_commit", "")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
            raise ValueError(f"{ident}: done requires a full implementation_commit hash")
        rounds = entry.get("review_rounds")
        if type(rounds) is not int or rounds < 1:
            raise ValueError(f"{ident}: done requires review_rounds >= 1")
        anchor = entry.get("log_anchor", "")
        if not isinstance(anchor, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", anchor):
            raise ValueError(f"{ident}: done requires a log_anchor")
        log = (root / "docs/implementation-log.md").read_text(encoding="utf-8")
        section = re.search(rf"^## {re.escape(anchor)}\n(.*?)(?=^## |\Z)", log, re.M | re.S)
        if not section or commit not in section.group(1) or ident not in section.group(1):
            raise ValueError(f"{ident}: log section must contain the ticket ID and implementation_commit")
        kind = subprocess.run(["git", "cat-file", "-t", commit], cwd=root, capture_output=True, text=True)
        reachable = subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=root, capture_output=True)
        if kind.returncode or kind.stdout.strip() != "commit" or reachable.returncode:
            raise ValueError(f"{ident}: implementation_commit is not a commit in the current HEAD history")
    for ident, ticket in tickets.items():
        if entries[ident]["status"] == "done":
            unmet = [dep for dep in ticket["dependencies"] if entries[dep]["status"] != "done"]
            if unmet:
                raise ValueError(f"{ident}: done with unfinished dependencies: {unmet}")
    return entries


def select_ticket(tickets: dict, entries: dict, order: list[str], requested: str | None) -> tuple[dict, int]:
    if requested is not None and requested not in tickets:
        raise ValueError(f"Unknown ticket: {requested}")
    ident = requested or next((item for item in order if entries[item]["status"] != "done"), None)
    if ident is None or entries[ident]["status"] == "done":
        return {"ticket": ident, "result": "already_done" if ident else "all_done"}, 4
    unmet = [dep for dep in tickets[ident]["dependencies"] if entries[dep]["status"] != "done"]
    return {
        "ticket": ident,
        **tickets[ident],
        "status": entries[ident]["status"],
        "blocker": entries[ident].get("blocker"),
        "unmet_dependencies": unmet,
        "result": "dependencies_incomplete" if unmet else "selected",
    }, 3 if unmet else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["next", "order", "check"])
    parser.add_argument("--ticket", help="Explicit ticket ID (next only)")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root; defaults to cwd")
    args = parser.parse_args()
    if args.ticket and args.command != "next":
        parser.error("--ticket is only supported by next")
    try:
        root = args.root.resolve()
        tickets = parse_tickets((root / "docs/08-implementation-tickets.md").read_text(encoding="utf-8"))
        order = topological_order(tickets)
        data = json.loads((root / "docs/implementation-status.json").read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        entries = validate_status(data, tickets, root)
        code = 0
        if args.command == "next":
            result, code = select_ticket(tickets, entries, order, args.ticket)
        elif args.command == "order":
            result = [{"ticket": ident, "status": entries[ident]["status"], "dependencies": tickets[ident]["dependencies"]} for ident in order]
        else:
            result = {"valid": True, "tickets": len(tickets), "counts": {s: sum(e["status"] == s for e in entries.values()) for s in sorted(STATUSES)}}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (ValueError, OSError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
