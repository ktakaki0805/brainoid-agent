"""Run with: python3 -B -m unittest discover -s <skill>/scripts -p 'test_*.py'."""

import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import ticket_plan as planner


class TicketPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ticket-plan-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "docs").mkdir()
        # The document deliberately lists a dependent before its prerequisite.
        self.document = """# Tickets
#### BA-FND-02 second
| 依存PR | [BA-FND-01](#first) |
#### BA-FND-01 first
| 依存PR | なし |
#### BA-OPS-01 independent
| 依存PR | なし |
"""
        self.tickets = planner.parse_tickets(self.document)
        self.data = {"schema_version": 1, "tickets": {ident: {"status": "todo"} for ident in self.tickets}}
        self.save()

    def save(self):
        (self.root / "docs/08-implementation-tickets.md").write_text(self.document)
        (self.root / "docs/implementation-status.json").write_text(json.dumps(self.data))

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def committed_state(self):
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Fixture")
        (self.root / "implementation.txt").write_text("implemented\n")
        self.git("add", "implementation.txt")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "feat(fixture): implement BA-FND-01")
        commit = self.git("rev-parse", "HEAD")
        self.data["tickets"]["BA-FND-01"] = {
            "status": "done", "implementation_commit": commit,
            "log_anchor": "ba-fnd-01-20261025t120000z", "review_rounds": 2,
            "updated_at": "2026-10-25T21:00:00+09:00",
        }
        self.write_log(commit)
        return commit

    def write_log(self, commit):
        (self.root / "docs/implementation-log.md").write_text(
            f"# Log\n\n## ba-fnd-01-20261025t120000z\n\n- チケット: BA-FND-01\n- 実装コミット: {commit}\n"
        )

    def invoke(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["ticket_plan.py", *args, "--root", str(self.root)]):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                code = planner.main()
        return code, json.loads(output.getvalue() or error.getvalue())

    def test_topology_and_document_order_tiebreak(self):
        self.assertEqual(planner.topological_order(self.tickets), ["BA-FND-01", "BA-FND-02", "BA-OPS-01"])

    def test_default_selects_one_and_does_not_write(self):
        before = {p: p.read_bytes() for p in (self.root / "docs").iterdir()}
        code, result = self.invoke("next")
        self.assertEqual(code, 0)
        self.assertEqual(result["ticket"], "BA-FND-01")
        self.assertEqual(before, {p: p.read_bytes() for p in (self.root / "docs").iterdir()})

    def test_explicit_ticket_does_not_substitute_dependency(self):
        code, result = self.invoke("next", "--ticket", "BA-FND-02")
        self.assertEqual(code, 3)
        self.assertEqual(result["ticket"], "BA-FND-02")
        self.assertEqual(result["unmet_dependencies"], ["BA-FND-01"])
        self.assertEqual(self.invoke("next", "--ticket", "BA-OPS-01")[0], 0)

    def test_unknown_ticket_rejected(self):
        self.assertEqual(self.invoke("next", "--ticket", "BA-NO-01")[0], 2)

    def test_resume_and_blocked_are_not_skipped(self):
        for state in ("in_progress", "in_review", "blocked"):
            with self.subTest(state=state):
                self.data["tickets"]["BA-FND-01"] = {
                    "status": state, "updated_at": "2026-10-25T12:00:00Z", "blocker": "Waiting for fixture"
                }
                self.save()
                code, result = self.invoke("next")
                self.assertEqual(code, 0)
                self.assertEqual(result["ticket"], "BA-FND-01")
                self.assertEqual(result["status"], state)

    def test_cycle_rejected(self):
        cyclic = deepcopy(self.tickets)
        cyclic["BA-FND-01"]["dependencies"] = ["BA-FND-02"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            planner.topological_order(cyclic)

    def test_missing_unknown_and_duplicate_dependencies_rejected(self):
        invalid = [
            self.document.replace("| 依存PR | なし |", "| other | value |", 1),
            self.document.replace("| 依存PR | なし |", "| 依存PR | BA-NO-01 |", 1),
            self.document.replace("[BA-FND-01](#first)", "BA-FND-01、BA-FND-01"),
            self.document.replace("| 依存PR | なし |", "| 依存PR | 未定 |", 1),
        ]
        for document in invalid:
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    planner.parse_tickets(document)

    def test_duplicate_headings_rejected_and_fenced_examples_ignored(self):
        with self.assertRaisesRegex(ValueError, "Duplicate ticket"):
            planner.parse_tickets(self.document + self.document)
        fenced = self.document + "\n```markdown\n#### BA-NO-01 example\n| 依存PR | なし |\n```\n"
        self.assertEqual(planner.parse_tickets(fenced), self.tickets)

    def test_status_ids_and_schema_rejected_when_corrupt(self):
        invalid = [
            {"schema_version": True, "tickets": self.data["tickets"]},
            {"schema_version": 1, "tickets": {}},
            {"schema_version": 1, "tickets": {**self.data["tickets"], "BA-NO-01": {"status": "todo"}}},
        ]
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    planner.validate_status(data, self.tickets, self.root)

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            json.loads('{"tickets": {}, "tickets": {}}', object_pairs_hook=planner.unique_object)

    def test_missing_status_file_is_not_treated_as_all_todo(self):
        (self.root / "docs/implementation-status.json").unlink()
        self.assertEqual(self.invoke("next")[0], 2)

    def test_done_requires_commit_log_and_review_evidence(self):
        self.committed_state()
        self.assertEqual(planner.validate_status(self.data, self.tickets, self.root), self.data["tickets"])
        for key in ("implementation_commit", "log_anchor", "review_rounds", "updated_at"):
            with self.subTest(key=key):
                data = deepcopy(self.data)
                del data["tickets"]["BA-FND-01"][key]
                with self.assertRaises(ValueError):
                    planner.validate_status(data, self.tickets, self.root)
        self.write_log("0" * 40)
        with self.assertRaisesRegex(ValueError, "log section"):
            planner.validate_status(self.data, self.tickets, self.root)

    def test_done_advances_and_explicit_done_is_noop(self):
        self.committed_state()
        self.save()
        self.assertEqual(self.invoke("next")[1]["ticket"], "BA-FND-02")
        code, result = self.invoke("next", "--ticket", "BA-FND-01")
        self.assertEqual((code, result["result"]), (4, "already_done"))

    def test_commit_from_other_branch_does_not_satisfy_dependency(self):
        self.committed_state()
        self.git("switch", "--orphan", "unrelated")
        self.git("-c", "core.hooksPath=/dev/null", "commit", "--allow-empty", "-qm", "chore: unrelated")
        with self.assertRaisesRegex(ValueError, "current HEAD history"):
            planner.validate_status(self.data, self.tickets, self.root)

    def test_done_cannot_have_unfinished_dependencies(self):
        commit = self.committed_state()
        self.data["tickets"]["BA-FND-02"] = deepcopy(self.data["tickets"]["BA-FND-01"])
        self.data["tickets"]["BA-FND-01"] = {"status": "todo"}
        log = self.root / "docs/implementation-log.md"
        log.write_text(log.read_text().replace("チケット: BA-FND-01", "チケット: BA-FND-02"))
        with self.assertRaisesRegex(ValueError, "unfinished dependencies"):
            planner.validate_status(self.data, self.tickets, self.root)
        self.assertIn(commit, log.read_text())

    def test_all_done_has_no_next_ticket(self):
        entries = {ident: {"status": "done"} for ident in self.tickets}
        result, code = planner.select_ticket(self.tickets, entries, planner.topological_order(self.tickets), None)
        self.assertEqual((code, result["result"]), (4, "all_done"))

    def test_timestamp_must_have_timezone_and_blocked_needs_reason(self):
        for fields in (
            {"status": "in_review", "updated_at": "2026-10-25T12:00:00"},
            {"status": "blocked", "updated_at": "2026-10-25T12:00:00Z"},
            {"status": "unrecognized"},
        ):
            with self.subTest(fields=fields):
                data = deepcopy(self.data)
                data["tickets"]["BA-FND-01"] = fields
                with self.assertRaises(ValueError):
                    planner.validate_status(data, self.tickets, self.root)

    def test_blocker_must_be_a_nonempty_string(self):
        for blocker in (None, False, 0, [], {}, "", "   "):
            with self.subTest(blocker=blocker):
                self.data["tickets"]["BA-FND-01"] = {
                    "status": "blocked", "updated_at": "2026-10-25T12:00:00Z", "blocker": blocker,
                }
                self.save()
                code, result = self.invoke("check")
                self.assertEqual(code, 2)
                self.assertIn("blocked requires a blocker", result["error"])


if __name__ == "__main__":
    unittest.main()
