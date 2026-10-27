"""Actual Pi proposals consumed by owner HTTP/SSE across delayed release boundaries."""

import asyncio
from datetime import timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import brainoid_api.repositories.conversations as conversation_repository
import brainoid_api.routes.conversations as conversation_routes
import brainoid_api.routes.run_events as event_routes
import brainoid_memory.calendar_proposal_access as proposal_access
from brainoid_api.app import create_app
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.routes.agents import reader_identity
from brainoid_api.services.private_sessions import PrivateSessionCreate
from brainoid_api.settings import load_settings
from brainoid_contracts import DisplayPolicy, ResourceId, Visibility
from brainoid_contracts.guards import GuardedTransaction
from brainoid_memory.calendar_access import check_calendar_sources
from brainoid_policy.scopes import PolicyDenied, PolicyUnavailable
from brainoid_runs.models import ConversationCreate, TurnCreate
from brainoid_runs.state import failed_projection
from starlette.concurrency import run_in_threadpool
from starlette.testclient import TestClient
from test_api_foundation import LOCAL_ENV
from test_memory_revalidation import MatchedClock


class ProposalReleaseScenarios:
    def release_advance(self: Any) -> None:
        if hasattr(self, "clock"):
            self.clock.advance(60)
        else:
            self.now += timedelta(seconds=60)

    def release_fixture(
        self: Any, *, completed: bool = False
    ) -> tuple[Any, ResourceId, ResourceId]:
        if hasattr(self, "credentials"):
            credentials = self.credentials()
        else:
            pairing = self.pairing()
            self.confirm(pairing)
            credentials = self.exchange(pairing)
        path = f"tenants/{self.tenant}/devices/{credentials.device_id}"

        def configure(tx: Any) -> None:
            tx.set(
                path,
                {
                    **tx.get(path),
                    "capabilities": ["display"],
                    "allowed_scopes": ["conversation", "calendar.propose"],
                },
            )

        self.store.run(configure)
        identity = self.repo.authenticate(credentials.access_token)
        session = self.sessions.start(
            self.identity(),
            self.tenant,
            identity.sub,
            PrivateSessionCreate(
                acknowledge_shared_space=True,
                allowed_scopes=["conversation", "calendar.propose"],
            ),
            str(uuid4()),
            "calendar-release-test",
        )
        self.release_pi, self.release_session = identity, session.id
        parent = self.conversations.create(
            self.identity(),
            self.tenant,
            ConversationCreate(
                agent_id=ResourceId(
                    self.arguments.get("agent_id") or getattr(self, "agent_id", None) or self.agent
                ),
                visibility=Visibility.PRIVATE,
                display_policy=DisplayPolicy.AUTHENTICATED_SESSION,
            ),
            str(uuid4()),
        ).id
        self.handler.clock = MatchedClock(self.jobs.now)
        run = self.conversations.turn(
            identity,
            self.tenant,
            parent,
            TurnCreate(text="SYNTHETIC_PRIVATE_PI_INPUT", output_mode="text"),
            str(uuid4()),
            session.id,
        ).run_id
        self.execute(run)
        public = self.conversations.get_run(self.identity(), self.tenant, run)
        self.assertEqual(public.status, "waiting_confirmation")
        if completed:
            self.proposals.reject(
                self.identity(), self.tenant, public.waiting_proposal_id, str(uuid4())
            )
            self.execute(run)
            self.assertTrue(
                any(
                    item.role == "assistant"
                    for item in self.conversations.messages(
                        self.identity(), self.tenant, parent
                    ).items
                )
            )
        settings = (
            load_settings(LOCAL_ENV)
            if hasattr(self, "clock")
            else load_settings().model_copy(update={"authz_ledger_directory": self.directory})
        )
        app = create_app(settings_loader=lambda: settings)
        app.dependency_overrides[authenticated_identity] = lambda: self.identity()
        app.dependency_overrides[reader_identity] = lambda: self.identity()
        app.dependency_overrides[get_auth_services] = lambda: AuthServices(
            tenants=self.tenants,
            devices=self.repo,
            private_sessions=self.sessions,
            conversations=self.conversations,
            calendar_events=self.proposals.events,
        )
        client = self.enterContext(TestClient(app))
        return client, parent, run

    def test_owner_run_final_read_retains_original_pi_deadline(self: Any) -> None:
        client, _, run = self.release_fixture()
        original = failed_projection

        def delayed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            self.release_advance()
            return result

        with patch.object(conversation_repository, "failed_projection", delayed):
            response = client.get(f"/v1/tenants/{self.tenant}/runs/{run}")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("waiting_proposal_id", response.text)

    def test_owner_history_final_read_denies_then_omits_expired_item(self: Any) -> None:
        client, parent, _ = self.release_fixture(completed=True)
        original = check_calendar_sources

        def delayed(*args: Any, **kwargs: Any) -> Any:
            result = original(*args, **kwargs)
            if args[3].get("calendar_sources"):
                self.release_advance()
            return result

        url = f"/v1/tenants/{self.tenant}/conversations/{parent}/messages"
        with patch.object(conversation_repository, "check_calendar_sources", delayed):
            response = client.get(url)
        self.assertEqual(response.status_code, 404, response.text)
        fresh = client.get(url)
        self.assertEqual(fresh.status_code, 200, fresh.text)
        self.assertEqual([m["role"] for m in fresh.json()["items"]], ["user"])

    def release_http_response(self: Any, *, history: bool, threadpool: bool) -> None:
        client, parent, run = self.release_fixture(completed=history)
        url = (
            f"/v1/tenants/{self.tenant}/conversations/{parent}/messages"
            if history
            else f"/v1/tenants/{self.tenant}/runs/{run}"
        )
        calls = []
        if threadpool:
            original = run_in_threadpool

            async def delayed(*args: Any, **kwargs: Any) -> Any:
                result = await original(*args, **kwargs)
                calls.append(True)
                self.release_advance()
                return result

            target, name, delay = conversation_routes, "run_in_threadpool", delayed
        else:
            original_run = self.store.run

            def delayed_run(*args: Any, **kwargs: Any) -> Any:
                result = original_run(*args, **kwargs)
                calls.append(True)
                self.release_advance()
                return result

            target, name, delay = self.store, "run", delayed_run
        with patch.object(target, name, delay):
            response = client.get(url)
        self.assertEqual(len(calls), 1)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("FIXTURE_stage_0", response.text)

    def test_owner_run_transaction_response_deadline(self: Any) -> None:
        self.release_http_response(history=False, threadpool=False)

    def test_owner_history_transaction_response_deadline(self: Any) -> None:
        self.release_http_response(history=True, threadpool=False)

    def test_owner_run_threadpool_response_deadline(self: Any) -> None:
        self.release_http_response(history=False, threadpool=True)

    def test_owner_history_threadpool_response_deadline(self: Any) -> None:
        self.release_http_response(history=True, threadpool=True)

    def test_proposal_http_transaction_response_deadline_is_normalized(self: Any) -> None:
        client, _, run = self.release_fixture()
        proposal = self.conversations.get_run(self.identity(), self.tenant, run).waiting_proposal_id
        original = self.store.run
        calls = []

        def delayed(work: Any) -> Any:
            result = original(work)
            calls.append(True)
            self.release_advance()
            return result

        with patch.object(self.store, "run", delayed):
            response = client.get(f"/v1/tenants/{self.tenant}/calendar-proposals/{proposal}")
        self.assertEqual(len(calls), 1)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("normalized_request", response.text)

    def release_stream_response(self: Any, *, threadpool: bool, unavailable: bool = False) -> None:
        _, _, run = self.release_fixture()
        calls = []
        original = self.store.run

        def delayed(work: Any) -> Any:
            result = original(work)
            calls.append(True)
            if len(calls) == 3:
                if unavailable:
                    self.assertIsInstance(result[1], GuardedTransaction)

                    def denied() -> None:
                        raise PolicyUnavailable()

                    result[1].checks.append(denied)
                else:
                    self.release_advance()
            return result

        original_pool = run_in_threadpool

        async def delayed_pool(*args: Any, **kwargs: Any) -> Any:
            result = await original_pool(*args, **kwargs)
            calls.append(True)
            self.release_advance()
            return result

        async def consume() -> None:
            iterator = event_routes.stream(
                self.conversations, self.identity(), self.tenant, run, None, 1
            )
            try:
                with patch.object(
                    event_routes if threadpool else self.store,
                    "run_in_threadpool" if threadpool else "run",
                    delayed_pool if threadpool else delayed,
                ):
                    self.assertEqual([frame async for frame in iterator], [])
            finally:
                await iterator.aclose()

        asyncio.run(consume())
        self.assertEqual(len(calls), 1 if threadpool else 3)

    def test_sse_final_delivery_response_deadline(self: Any) -> None:
        self.release_stream_response(threadpool=False)

    def test_sse_threadpool_response_deadline(self: Any) -> None:
        self.release_stream_response(threadpool=True)

    def test_sse_post_response_unavailable_closes_without_payload(self: Any) -> None:
        self.release_stream_response(threadpool=False, unavailable=True)

    def test_http_post_response_policy_error_is_normalized(self: Any) -> None:
        client, _, run = self.release_fixture()
        original = self.store.run

        def delayed_error(error: type[PolicyDenied] | type[PolicyUnavailable]) -> Any:
            def delayed(work: Any) -> Any:
                result = original(work)
                self.assertIsInstance(result[1], GuardedTransaction)

                def denied() -> None:
                    raise error()

                result[1].checks.append(denied)
                return result

            return delayed

        for error, status in ((PolicyDenied, 404), (PolicyUnavailable, 503)):
            with self.subTest(error=error.__name__):
                with patch.object(self.store, "run", delayed_error(error)):
                    response = client.get(f"/v1/tenants/{self.tenant}/runs/{run}")
                self.assertEqual(response.status_code, status, response.text)
                self.assertNotIn("waiting_proposal_id", response.text)

    def test_denied_history_pass_does_not_retain_partial_guards(self: Any) -> None:
        client, parent, _ = self.release_fixture(completed=True)
        original = proposal_access._check_origin

        def denied_after_evidence(*args: Any, **kwargs: Any) -> Any:
            original(*args, **kwargs)
            # Simulate a later source failing after this ancestor's valid evidence.
            # A denied item's partial guard must not contaminate the remaining page.
            self.release_advance()
            raise PolicyDenied()

        with patch.object(proposal_access, "_check_origin", denied_after_evidence):
            response = client.get(f"/v1/tenants/{self.tenant}/conversations/{parent}/messages")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([m["role"] for m in response.json()["items"]], ["user"])
