"""Render-test inputs from real usage/turn routes; no emulator or external provider needed."""

import json

from brainoid_usage import Policy
from test_turn_run import TurnRunFixture


def cases() -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for role in ("owner", "admin", "member"):
        for limit in ("turns", "concurrent_runs"):
            fixture = TurnRunFixture()
            try:
                fixture.setUp()
                fixture.uid = "demo-a-" + role
                policy = Policy().model_dump()
                policy["user"][limit] = 0
                fixture.store.data[f"tenants/{fixture.tenant}/usage_policy/current"] = policy
                usage = fixture.client.get(f"/v1/tenants/{fixture.tenant}/usage")
                fixture.assertEqual(usage.status_code, 200)
                expected_remaining = 10000 if role != "member" else 0 if limit == "turns" else 2000
                expected_state = (
                    "available"
                    if role != "member"
                    else "exhausted"
                    if limit == "turns"
                    else "limited"
                )
                fixture.assertEqual(usage.json()["remaining_turns"], expected_remaining)
                fixture.assertEqual(usage.json()["quota_state"], expected_state)
                turn = fixture.turn(fixture.conversation())
                fixture.assertEqual(turn.status_code, 429)
                fixture.assertEqual(turn.headers["Retry-After"], "60")
                results.append(
                    {
                        "role": role,
                        "user_limit": limit,
                        "usage": usage.json(),
                        "turn_status": turn.status_code,
                        "retry_after": turn.headers["Retry-After"],
                    }
                )
            finally:
                fixture.doCleanups()
    return results


if __name__ == "__main__":
    print(json.dumps(cases()))
