"""MEM-03 / T-008 / T-010: durable explicit memories and current provenance."""

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import yaml  # type: ignore[import-untyped]
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import ErrorCode
from brainoid_api.repositories.memories import Memories, MemoryCreate, MemoryRevision
from brainoid_api.routes.agents import reader_identity
from brainoid_contracts import FirebaseUid, ResourceId
from brainoid_jobs.models import JobError, job_id
from brainoid_memory.models import Fact
from brainoid_policy.models import ResourceKind
from brainoid_policy.scopes import PolicyUnavailable
from fakes.memory_repository import CONVERSATION, NOW, OTHER, SOURCE, TENANT, USER
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from test_entities import EntityFixture

KEY = "memory-create-key-001"


class MemoryFixture(EntityFixture):
    def setUp(self) -> None:
        super().setUp()
        self.subject = self.create_entity(kind="person").json()["id"]
        self.memories = Memories(
            self.store,
            self.tenants,
            self.cursors,
            ledger=self.ledger,
            credentials=self.credentials,
            now=lambda: self.now,
            allow_untracked_bootstrap=True,
        )
        self.app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.credentials,
            entities=self.entities,
            memories=self.memories,
        )
        self.url = f"/v1/tenants/{TENANT}/memories"
        self.body: dict[str, Any] = {
            "subject_entity_id": self.subject,
            "predicate": "prefers",
            "value": "tea",
            "visibility": "shared",
            "display_policy": "shared_display",
        }

    def create_memory(self, key: str = KEY, **changes: Any) -> Any:
        return self.client.post(
            self.url, json={**self.body, **changes}, headers={"Idempotency-Key": key}
        )

    def revise_memory(self, memory: str, key: str = "memory-revision-key", **changes: Any) -> Any:
        return self.client.patch(
            self.url + "/" + memory,
            json={"replacement": {**self.body, "value": "coffee", **changes}},
            headers={"Idempotency-Key": key, "If-Match": '"1"'},
        )

    def message(self, **parent_changes: Any) -> None:
        parent = copy.deepcopy(self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"])
        parent.update(id=CONVERSATION, **parent_changes)
        base = f"tenants/{TENANT}/conversations/{CONVERSATION}"
        self.store.data[base] = parent
        self.store.data[base + "/messages/" + SOURCE] = {
            "id": SOURCE,
            "tenant_id": TENANT,
            "conversation_id": CONVERSATION,
            "version": 1,
            "deletion_epoch": 0,
            "deleted": False,
            "suppress_reuse": False,
            "role": "user",
            "run_id": str(uuid4()),
            "text": "synthetic source",
            "created_at": NOW,
        }


class MemoryTests(MemoryFixture):
    def test_immediate_get_direct_provenance_atomic_job_and_receipt_contract(self) -> None:
        response = self.create_memory()
        self.assertEqual(response.status_code, 201, response.text)
        saved = response.json()
        self.assertEqual(saved["origin"], "user_explicit")
        self.assertEqual(saved["status"], "confirmed")
        self.assertEqual(saved["source_refs"], [])
        self.assertEqual(saved["created_by"], USER)
        self.assertEqual(saved["valid_from"], saved["recorded_at"])
        self.assertEqual(response.headers["etag"], '"1"')
        self.assertEqual(self.client.get(self.url + "/" + saved["id"]).json(), saved)
        self.assertEqual(self.create_memory().json(), saved)
        page = self.client.get(self.url).json()
        self.assertEqual(page, {"items": [saved], "next_cursor": None})
        schema = yaml.safe_load(Path("specs/openapi.yaml").read_text())
        Draft202012Validator(
            {"$ref": "#/components/schemas/MemoryPage", "components": schema["components"]}
        ).validate(page)
        jobs = [d for p, d in self.store.data.items() if "/jobs/" in p]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "waiting")
        self.assertEqual(jobs[0]["grant"]["resource"]["ref"]["id"], saved["id"])
        self.assertIsNone(self.memories.jobs.prepare_dispatch(TENANT, jobs[0]["id"]))
        self.assertEqual(len([p for p in self.store.data if "/idempotency_records/" in p]), 2)
        self.assertEqual(len([p for p in self.store.data if "/facts/" in p]), 1)

    def test_revision_history_stale_match_replay_and_confirmed_list(self) -> None:
        old = self.create_memory().json()
        response = self.revise_memory(old["id"])
        self.assertEqual(response.status_code, 200, response.text)
        new = response.json()
        self.assertNotEqual(old["id"], new["id"])
        self.assertEqual(new["supersedes_fact_id"], old["id"])
        prior = self.client.get(self.url + "/" + old["id"])
        self.assertEqual((prior.json()["status"], prior.headers["etag"]), ("superseded", '"2"'))
        self.assertEqual(self.client.get(self.url).json()["items"], [new])
        self.assertEqual(self.revise_memory(old["id"]).json(), new)
        self.assertEqual(self.revise_memory(old["id"], "memory-stale-revision").status_code, 412)
        self.assertEqual(self.revise_memory(old["id"], value="changed").status_code, 409)
        # A delayed create retry returns history explicitly, never confirmed old content.
        self.assertEqual(self.create_memory().json()["status"], "superseded")

    def test_request_values_server_fields_and_business_constraints(self) -> None:
        for changes in (
            {"origin": "imported"},
            {"status": "confirmed"},
            {"owner_user_id": "other"},
            {"created_by": "other"},
            {"value": None},
            {"object_entity_id": None},
            {"object_entity_id": str(uuid4())},
            {"valid_from": None},
            {"qualifiers": {"brightness_pct": 20}},
            {"qualifiers": {"context": None}},
            {
                "source_refs": [
                    {
                        "kind": "note",
                        "resource_id": SOURCE,
                        "version": 1,
                        "path": "tenants/other/notes/private",
                    }
                ]
            },
            {"source_refs": [{"kind": "note", "resource_id": SOURCE, "version": 1}] * 2},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(self.create_memory(**changes).status_code, 422)
        self.assertEqual(self.create_memory(valid_to=NOW.isoformat()).status_code, 400)
        self.assertEqual(self.client.post(self.url, json=self.body).status_code, 400)
        memory = self.create_memory().json()["id"]
        self.assertEqual(
            self.client.patch(self.url + "/" + memory, json={"replacement": self.body}).status_code,
            428,
        )

    def test_nonexistent_source_object_cross_tenant_and_private_source_denied(self) -> None:
        for source in (
            {"kind": "note", "resource_id": str(uuid4()), "version": 1},
            {"kind": "message", "resource_id": SOURCE, "version": 1},
            {"kind": "import", "resource_id": SOURCE, "version": 1},
        ):
            self.assertEqual(self.create_memory(source_refs=[source]).status_code, 404)
        self.assertEqual(self.create_memory(subject_entity_id=str(uuid4())).status_code, 404)
        self.assertEqual(
            self.client.post(
                f"/v1/tenants/{OTHER}/memories", json=self.body, headers={"Idempotency-Key": KEY}
            ).status_code,
            404,
        )
        note = self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"]
        note.update(visibility="private", display_policy="never")
        refs = [{"kind": "note", "resource_id": SOURCE, "version": 1}]
        self.assertEqual(self.create_memory(source_refs=refs).status_code, 404)
        self.assertEqual(
            self.create_memory(
                source_refs=refs, visibility="private", display_policy="never"
            ).status_code,
            201,
        )
        note = self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"]
        note["owner_user_id"] = "someone-else"
        self.assertEqual(
            self.create_memory(
                source_refs=refs, visibility="private", display_policy="never"
            ).status_code,
            404,
        )

    def test_message_real_parent_current_scope_version_epoch_suppression(self) -> None:
        self.message()
        refs = [{"kind": "message", "resource_id": SOURCE, "version": 1}]
        created = self.create_memory(source_refs=refs)
        self.assertEqual(created.status_code, 201, created.text)
        memory = created.json()["id"]
        snapshot = copy.deepcopy(self.store.data)
        base = f"tenants/{TENANT}/conversations/{CONVERSATION}"
        for suffix, changes in (
            ("", {"visibility": "private", "display_policy": "never"}),
            ("", {"deleted": True}),
            ("", {"suppress_reuse": True}),
            ("/messages/" + SOURCE, {"version": 2}),
            ("/messages/" + SOURCE, {"deleted": True}),
            ("/messages/" + SOURCE, {"suppress_reuse": True}),
            ("/messages/" + SOURCE, {"deletion_epoch": 1}),
            ("/messages/" + SOURCE, {"conversation_id": str(uuid4())}),
        ):
            self.store.data = copy.deepcopy(snapshot)
            self.store.data[base + suffix].update(changes)
            with self.subTest(changes=changes):
                self.assertEqual(
                    self.create_memory("memory-source-change", source_refs=refs).status_code, 404
                )
                if "suppress_reuse" not in changes and "deletion_epoch" not in changes:
                    self.assertEqual(self.client.get(self.url + "/" + memory).status_code, 404)
                    self.assertEqual(self.client.get(self.url).json()["items"], [])
        self.store.data = copy.deepcopy(snapshot)
        other = str(uuid4())
        self.store.data[f"tenants/{TENANT}/conversations/{other}"] = {
            **self.store.data[base],
            "id": other,
        }
        self.store.data[f"tenants/{TENANT}/conversations/{other}/messages/{SOURCE}"] = {
            **self.store.data[base + "/messages/" + SOURCE],
            "conversation_id": other,
        }
        self.assertEqual(self.create_memory(source_refs=refs).status_code, 404)

    def test_current_entity_changes_and_revision_scope_do_not_publish_private_ids(self) -> None:
        old = self.create_memory(visibility="private", display_policy="never").json()
        self.assertEqual(self.revise_memory(old["id"]).status_code, 404)
        replacement = self.revise_memory(old["id"], visibility="private", display_policy="never")
        self.assertEqual(replacement.status_code, 200, replacement.text)
        self.assertEqual(replacement.json()["owner_user_id"], USER)
        self.uid = FirebaseUid("other-owner")
        self.store.data[f"tenants/{TENANT}/memberships/other"] = {
            "tenant_id": TENANT,
            "user_id": self.uid,
            "role": "owner",
            "status": "active",
            "authorization_version": 1,
        }
        self.assertEqual(self.client.get(self.url).json()["items"], [])
        self.assertEqual(
            self.client.get(self.url + "/" + replacement.json()["id"]).status_code, 404
        )
        self.uid = USER
        shared = self.create_memory("memory-shared-key").json()
        path = f"tenants/{TENANT}/entities/{self.subject}"
        self.store.data[path].update(visibility="private", display_policy="never")
        self.assertEqual(self.client.get(self.url + "/" + shared["id"]).status_code, 404)
        self.assertNotIn(shared["id"], str(self.client.get(self.url).json()))

    def test_revision_nullable_shared_owner_and_link_current_scope(self) -> None:
        old = self.create_memory().json()
        path = f"tenants/{TENANT}/facts/{old['id']}"
        self.store.data[path]["owner_user_id"] = None
        new = self.revise_memory(old["id"])
        self.assertEqual(new.status_code, 200, new.text)
        self.assertNotIn("owner_user_id", new.json())
        self.assertIsNone(Fact.model_validate(self.store.data[path]).owner_user_id)
        self.store.data[path].update(
            visibility="private", owner_user_id=USER, display_policy="never"
        )
        self.assertEqual(self.client.get(self.url + "/" + new.json()["id"]).status_code, 404)
        self.assertEqual(self.client.get(self.url).json()["items"], [])

    def test_commit_failure_and_retry_revocation_leave_no_partial_state(self) -> None:
        before = copy.deepcopy(self.store.data)
        self.store.fail_commit = True
        with self.assertRaises(RuntimeError):
            self.memories.create(
                self.identity(), TENANT, MemoryCreate.model_validate(self.body), KEY
            )
        self.assertEqual(self.store.data, before)
        self.store.fail_commit = False
        self.store.retry = lambda: self.store.data[f"tenants/{TENANT}/notes/{SOURCE}"].update(
            deleted=True
        )
        self.assertEqual(
            self.create_memory(
                source_refs=[{"kind": "note", "resource_id": SOURCE, "version": 1}]
            ).status_code,
            404,
        )
        self.assertFalse(any("/facts/" in p or "/jobs/" in p for p in self.store.data))

    def test_revision_commit_failure_rolls_back_old_new_job_and_receipt(self) -> None:
        old = self.create_memory().json()
        before = copy.deepcopy(self.store.data)
        self.store.fail_commit = True
        with self.assertRaises(RuntimeError):
            self.memories.revise(
                self.identity(),
                TENANT,
                ResourceId(old["id"]),
                MemoryRevision(replacement=MemoryCreate.model_validate(self.body)),
                "memory-failed-revision",
                '"1"',
            )
        self.assertEqual(self.store.data, before)

    def test_deadline_after_last_job_read_and_commit_prevents_content(self) -> None:
        body = MemoryCreate.model_validate(self.body)
        identity = self.identity()
        before = copy.deepcopy(self.store.data)
        self.store.after_get = lambda path: (
            setattr(self, "now", identity.expires_at) if "/jobs/" in path else None
        )
        self.fails(
            lambda: self.memories.create(identity, TENANT, body, KEY), ErrorCode.RESOURCE_NOT_FOUND
        )
        self.assertEqual(self.store.data, before)
        self.store.after_get = lambda path: None
        self.now = NOW
        self.store.after_commit = lambda: setattr(self, "now", identity.expires_at)
        self.fails(
            lambda: self.memories.create(identity, TENANT, body, KEY), ErrorCode.RESOURCE_NOT_FOUND
        )
        self.assertEqual(len([p for p in self.store.data if "/jobs/" in p]), 1)
        self.store.after_commit = lambda: None
        self.now = NOW
        self.assertEqual(self.memories.create(identity, TENANT, body, KEY).status, "confirmed")

    def test_device_session_write_current_family_and_no_idle_extension(self) -> None:
        credentials = self.device()
        session = self.session(credentials.device_id)
        del self.app.dependency_overrides[reader_identity]
        headers = {
            "Authorization": "Bearer " + credentials.access_token,
            "X-Private-Session": session,
            "Idempotency-Key": KEY,
        }
        path = f"tenants/{TENANT}/private_sessions/{session}"
        before = copy.deepcopy(self.store.data[path])
        saved = self.client.post(
            self.url,
            json={**self.body, "visibility": "private", "display_policy": "authenticated_session"},
            headers=headers,
        )
        self.assertEqual(saved.status_code, 201, saved.text)
        self.assertEqual(saved.json()["created_by"], USER)
        self.assertEqual(self.store.data[path], before)
        self.assertEqual(
            self.client.post(
                self.url,
                json=self.body,
                headers={
                    "Authorization": headers["Authorization"],
                    "Idempotency-Key": "memory-unknown-speaker",
                },
            ).status_code,
            403,
        )
        identity = self.credentials.authenticate(credentials.access_token)
        self.store.data["device_credential_families/" + identity.family_id]["status"] = "revoked"
        self.assertEqual(self.client.get(self.url, headers=headers).status_code, 401)

    def test_list_cursor_hidden_changes_visible_changes_and_policy_outage(self) -> None:
        first = self.create_memory().json()
        self.create_memory("memory-second-key")
        page = self.client.get(self.url, params={"limit": 1}).json()
        self.assertIsNotNone(page["next_cursor"])
        self.assertEqual(
            self.client.get(
                self.url, params={"limit": 1, "cursor": page["next_cursor"]}
            ).status_code,
            200,
        )
        self.revise_memory(first["id"])
        self.assertEqual(
            self.client.get(
                self.url, params={"limit": 1, "cursor": page["next_cursor"]}
            ).status_code,
            400,
        )
        with patch(
            "brainoid_memory.repository.FactTransaction.get", side_effect=PolicyUnavailable()
        ):
            self.assertEqual(self.client.get(self.url).status_code, 503)

    def test_old_index_completion_cannot_restore_superseded_fact(self) -> None:
        old = self.create_memory().json()
        jid = job_id(TENANT, "memory_index", ResourceId(old["id"]))
        # Synthetic MEM-05 activation/handler; production jobs remain waiting.
        self.store.data[f"tenants/{TENANT}/jobs/{jid}"]["status"] = "pending"
        delivery = self.memories.jobs.prepare_dispatch(TENANT, jid)
        assert delivery is not None
        lease = self.memories.jobs.acquire(delivery, known_handler=lambda _: True)
        assert lease is not None
        self.revise_memory(old["id"])
        self.assertFalse(
            self.memories.jobs.complete(
                lease,
                None,
                lambda tx, job, result: tx.set(
                    f"tenants/{TENANT}/index/{old['id']}", {"value": "stale"}
                ),
            )
        )
        self.assertNotIn(f"tenants/{TENANT}/index/{old['id']}", self.store.data)
        self.assertEqual(self.memories.jobs.get(TENANT, jid).status, "obsolete")  # type: ignore[union-attr]

    def test_creation_job_rejects_other_resource_existing_source_and_mismatched_metadata(
        self,
    ) -> None:
        original = self.memories.jobs.add_created_memory
        before = copy.deepcopy(self.store.data)

        for corruption in ("version", "kind"):

            def tamper(
                tx: Any, *, grant: Any, source: Any, before_write: Any, mode: str = corruption
            ) -> Any:
                if mode == "version":
                    source = {**source, "version": 2}
                else:
                    grant = replace(
                        grant,
                        resource=grant.resource.model_copy(
                            update={
                                "ref": grant.ref.model_copy(update={"kind": ResourceKind.ENTITY})
                            }
                        ),
                    )
                return original(tx, grant=grant, source=source, before_write=before_write)

            with (
                self.subTest(corruption=corruption),
                patch.object(self.memories.jobs, "add_created_memory", side_effect=tamper),
                self.assertRaises(JobError),
            ):
                self.memories.create(
                    self.identity(), TENANT, MemoryCreate.model_validate(self.body), KEY
                )
            self.assertEqual(self.store.data, before)

        created = self.create_memory().json()
        saved = self.store.data[f"tenants/{TENANT}/facts/{created['id']}"]
        jid = job_id(TENANT, "memory_index", ResourceId(created["id"]))
        job = self.memories.jobs.get(TENANT, jid)
        assert job is not None
        before = copy.deepcopy(self.store.data)
        with self.assertRaises(JobError):
            self.store.run(
                lambda tx: original(tx, grant=job.grant, source=saved, before_write=lambda: None)
            )
        self.assertEqual(self.store.data, before)
