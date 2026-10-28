"""HTTP/worker observations consumed by React and SDK controller contract tests."""

import json
from collections.abc import Callable

from brainoid_api.app import create_app
from brainoid_api.auth.runtime import AuthServices, authenticated_identity, get_auth_services
from brainoid_api.common.pagination import Cursors
from brainoid_api.repositories.tenants import Tenants
from brainoid_api.settings import load_settings
from brainoid_contracts.deletion import subject_id
from starlette.testclient import TestClient
from test_api_foundation import LOCAL_ENV
from test_deletion_requests import USER, DeletionFixture


def services(fixture: DeletionFixture) -> Callable[[], AuthServices]:
    tenants = Tenants(
        fixture.store,
        Cursors(b"k" * 32, now=lambda: fixture.now),
        ledger=fixture.authz,
        now=lambda: fixture.now,
    )
    return lambda: AuthServices(tenants=tenants, deletion_requests=fixture.repo)


def cases() -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for kind in ("leave_tenant", "delete_account"):
        fixture = DeletionFixture()
        try:
            fixture.setUp()
            app = create_app(settings_loader=lambda: load_settings(LOCAL_ENV))
            app.dependency_overrides[authenticated_identity] = fixture.identity
            app.dependency_overrides[get_auth_services] = services(fixture)
            with TestClient(app) as client:
                url = (
                    "/v1/operations"
                    if kind == "delete_account"
                    else f"/v1/tenants/{fixture.tenant}/operations"
                )
                body = {"kind": kind, **({"confirm": True} if kind == "delete_account" else {})}
                response = client.post(
                    url, json=body, headers={"Idempotency-Key": "ui12-real-api-request-key"}
                )
                fixture.assertEqual(response.status_code, 202)
                accepted = response.json()
                operation = fixture.repo.get(
                    fixture.identity(),
                    accepted["operation_id"],
                    None if kind == "delete_account" else fixture.tenant,
                )
                fixture.drain(
                    operation, subject_id(USER) if kind == "delete_account" else fixture.tenant
                )
                after = client.get(url + "/" + accepted["operation_id"])
                fixture.assertEqual(after.status_code, 404 if kind == "delete_account" else 200)
                results.append(
                    {
                        "body": body,
                        "accepted": accepted,
                        "after_status": after.status_code,
                        "after": after.json(),
                    }
                )
        finally:
            fixture.doCleanups()
    return results


if __name__ == "__main__":
    print(json.dumps(cases()))
