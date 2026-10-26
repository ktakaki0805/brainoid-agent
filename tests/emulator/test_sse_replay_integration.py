"""T-023/T-032 with real Firestore transactions and local SSE transport."""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from brainoid_api.app import create_app
from brainoid_api.auth.runtime import AuthServices, get_auth_services
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.routes.agents import reader_identity
from brainoid_api.routes.run_events import next_batch, stream
from brainoid_contracts import ResourceId
from brainoid_persistence.firestore import PersistenceUnavailable
from brainoid_runs.state import conversation_path, run_path
from starlette.testclient import TestClient
from test_turn_run_integration import TurnRunIntegrationFixture


class SseReplayIntegrationTests(TurnRunIntegrationFixture):
    def events(self, run: ResourceId) -> list[dict[str, Any]]:
        return sorted(
            [d for _, d in self.persistence.page(run_path(self.tenant, run) + "/events")],
            key=lambda d: d["seq"],
        )

    def test_real_http_replay_schema_text_and_fixed_ttl(self) -> None:
        conversation = self.conversation()
        run = self.accept(conversation).run_id
        self.model.text = "日🙂" * 300
        self.execute(run)
        events = self.events(run)
        self.assertEqual(events[-1]["type"], "run.completed")
        self.assertEqual(
            "".join(e["payload"]["text"] for e in events if e["type"] == "text.delta"),
            self.conversations.messages(self.identity(), self.tenant, conversation).items[-1].text,
        )
        self.assertTrue(all(e["expires_at"] == self.now + timedelta(hours=24) for e in events))
        app = create_app()
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants, devices=self.repo, conversations=self.conversations
        )
        app.dependency_overrides[reader_identity] = self.identity
        with TestClient(app) as client:
            url = f"/v1/tenants/{self.tenant}/runs/{run}/events"
            response = client.get(url, headers={"Last-Event-ID": "1"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("id: 1\n", response.text)
            self.assertIn("event: run.completed", response.text)
            self.assertNotIn("PRIVATE_SIGNATURE", response.text)
            self.now += timedelta(hours=24)
            self.assertEqual(client.get(url).status_code, 410)
            self.assertEqual(client.get(url.removesuffix("/events")).status_code, 200)

    def test_disconnect_replay_and_deletion_between_batches(self) -> None:
        conversation = self.conversation()
        run = self.accept(conversation).run_id
        self.execute(run)

        async def check() -> None:
            first = stream(self.conversations, self.identity(), self.tenant, run, None, 0)
            self.assertIn("id: 1", await anext(first))
            await first.aclose()
            second = stream(self.conversations, self.identity(), self.tenant, run, None, 1)
            self.assertIn("id: 2", await anext(second))
            self.db.document(conversation_path(self.tenant, conversation)).update({"deleted": True})
            self.assertEqual([event async for event in second], [])

        asyncio.run(check())
        with self.assertRaises(APIError) as caught:
            next_batch(self.conversations, self.identity(), self.tenant, run, None, 2)
        self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_NOT_FOUND)

    def test_terminal_commit_loss_replay_preserves_events_and_final_message(self) -> None:
        conversation = self.conversation()
        run = self.accept(conversation).run_id
        self.model.text = "Fixture" * 400
        with patch.object(self.jobs, "complete", side_effect=PersistenceUnavailable()):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        before = self.events(run)
        self.assertNotIn("run.completed", [e["type"] for e in before])
        self.now += timedelta(seconds=6)
        complete = self.jobs.complete

        def lost(*args: Any, **kwargs: Any) -> bool:
            complete(*args, **kwargs)
            raise PersistenceUnavailable()

        with patch.object(self.jobs, "complete", side_effect=lost):
            with self.assertRaises(PersistenceUnavailable):
                self.execute(run)
        after = self.events(run)
        self.assertEqual(after[: len(before)], before)
        self.assertEqual(sum(e["type"] == "run.completed" for e in after), 1)
        self.assertEqual([e["seq"] for e in after], list(range(1, len(after) + 1)))
        self.assertEqual(len(self.model.requests), 1)
        self.assertEqual(
            len(self.conversations.messages(self.identity(), self.tenant, conversation).items), 2
        )

    def test_missing_event_and_old_history_return_gone(self) -> None:
        conversation = self.conversation()
        run = self.accept(conversation).run_id
        self.execute(run)
        self.db.document(run_path(self.tenant, run) + "/events/2").delete()
        with self.assertRaises(APIError) as caught:
            next_batch(self.conversations, self.identity(), self.tenant, run, None, 1)
        self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_EXPIRED)
        self.db.document(run_path(self.tenant, run)).update({"events_version": 0})
        with self.assertRaises(APIError) as caught:
            next_batch(self.conversations, self.identity(), self.tenant, run, None, 0)
        self.assertEqual(caught.exception.code, ErrorCode.RESOURCE_EXPIRED)
