"""Read/prepare then write, to compose with other Firestore transaction participants."""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid5

from brainoid_contracts import FirebaseUid, ResourceId, normalize_utc
from brainoid_contracts.transactions import Document, Transaction, TransactionStore

from brainoid_usage.models import Amounts, Limits, Policy, QuotaExceeded, Settlement, UsageView


def scope_key(kind: str, identity: str = "") -> str:
    # Firebase UIDs are opaque and may contain slashes; never interpolate them in paths.
    return kind + "-" + sha256(identity.encode()).hexdigest()


def bucket_path(tenant: ResourceId, period: str, scope: str) -> str:
    return f"tenants/{tenant}/usage_buckets/{period}-{scope}"


def run_path(tenant: ResourceId, run: ResourceId) -> str:
    return f"tenants/{tenant}/usage_runs/{run}"


def reservation_path(tenant: ResourceId, run: ResourceId, key: str) -> str:
    return f"tenants/{tenant}/usage_reservations/{uuid5(UUID(run), key)}"


def empty() -> Document:
    return {"turns": 0, "tokens": 0, "voice_seconds": 0}


def live_run(tx: Transaction, tenant: ResourceId, run: ResourceId, *, now: datetime) -> Document:
    """Shared read-only lifetime fence for model, tool and confirmation paths."""
    doc = tx.get(run_path(tenant, run))
    if (
        doc is None
        or not doc["active"]
        or normalize_utc(now) >= datetime.fromisoformat(doc["expires_at"])
    ):
        raise QuotaExceeded()
    return doc


class UsageLedger:
    def __init__(self, store: TransactionStore, *, now: Callable[[], datetime]) -> None:
        self.store, self.now = store, now

    @staticmethod
    def policy(tx: Transaction, tenant: ResourceId) -> Policy:
        return Policy.model_validate(tx.get(f"tenants/{tenant}/usage_policy/current") or {})

    def prepare_admit(
        self,
        tx: Transaction,
        tenant: ResourceId,
        run: ResourceId,
        *,
        user: FirebaseUid | None,
        device: ResourceId | None,
        purpose: Literal["run", "audio_input"] = "run",
        voice_seconds: int = 0,
    ) -> Callable[[], None]:
        if (purpose == "run" and voice_seconds != 0) or not 0 <= voice_seconds <= 60:
            raise ValueError("invalid_admission_units")
        policy = self.policy(tx, tenant)
        now = normalize_utc(self.now())
        period = now.strftime("%Y-%m")
        scopes = {scope_key("tenant"): "tenant"}
        if user is not None:
            scopes[scope_key("user", user)] = "user"
        if device is not None:
            scopes[scope_key("device", device)] = "device"
        if tx.get(run_path(tenant, run)) is not None:
            raise ValueError("usage_run_already_exists")
        writes: list[tuple[str, Document]] = []
        for scope, kind in scopes.items():
            limit: Limits = getattr(policy, kind)
            path = bucket_path(tenant, period, scope)
            bucket = tx.get(path) or empty()
            slot_path = f"tenants/{tenant}/usage_slots/{scope}"
            slots = tx.get(slot_path) or {"active": 0}
            if (
                (purpose == "run" and bucket["turns"] >= limit.turns)
                or slots["active"] >= limit.concurrent_runs
                or bucket["tokens"] >= limit.tokens
                or bucket["voice_seconds"] >= limit.voice_seconds
                or bucket["voice_seconds"] + voice_seconds > limit.voice_seconds
            ):
                raise QuotaExceeded()
            writes.extend(
                [
                    (
                        path,
                        {
                            **bucket,
                            "turns": bucket["turns"] + (purpose == "run"),
                            "voice_seconds": bucket["voice_seconds"] + voice_seconds,
                        },
                    ),
                    (slot_path, {"active": slots["active"] + 1}),
                ]
            )
        record = {
            "tenant_id": tenant,
            "run_id": run,
            "period": period,
            "scopes": scopes,
            "active": True,
            "reservation_keys": ["speech"] if voice_seconds else [],
            "purpose": purpose,
            "expires_at": (
                now + timedelta(seconds=60 if purpose == "audio_input" else policy.run_seconds)
            ).isoformat(),
        }

        def write() -> None:
            for path, doc in writes:
                tx.set(path, doc)
            tx.create(run_path(tenant, run), record)
            if voice_seconds:
                amounts = Amounts(voice_seconds=voice_seconds).model_dump()
                tx.create(
                    reservation_path(tenant, run, "speech"),
                    {
                        "tenant_id": tenant,
                        "run_id": run,
                        "period": period,
                        "scopes": scopes,
                        "reserved": amounts,
                        "charged": amounts,
                        "observed": Amounts().model_dump(),
                        "known": {name: value == 0 for name, value in amounts.items()},
                        "state": "started",
                    },
                )

        return write

    def reserve(
        self,
        tx: Transaction,
        tenant: ResourceId,
        run: ResourceId,
        key: str,
        amounts: Amounts,
    ) -> None:
        """Commit before calling a provider. Replays MUST NOT issue the external call again."""
        if re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", key) is None:
            raise ValueError("invalid_usage_key")
        active = live_run(tx, tenant, run, now=self.now())
        if len(active["reservation_keys"]) >= 64:
            raise QuotaExceeded()
        path = reservation_path(tenant, run, key)
        if tx.get(path) is not None:
            # Even an identical unresolved reservation is NOT permission to reexecute.
            raise QuotaExceeded()
        policy = self.policy(tx, tenant)
        # Charge to the call's UTC month; active slots are independent of month rollover.
        period = normalize_utc(self.now()).strftime("%Y-%m")
        writes = []
        for scope, kind in active["scopes"].items():
            limit: Limits = getattr(policy, kind)
            bucket_key = bucket_path(tenant, period, scope)
            bucket = tx.get(bucket_key) or empty()
            if (
                bucket["tokens"] + amounts.tokens > limit.tokens
                or bucket["voice_seconds"] + amounts.voice_seconds > limit.voice_seconds
            ):
                raise QuotaExceeded()
            writes.append(
                (
                    bucket_key,
                    {
                        **bucket,
                        "tokens": bucket["tokens"] + amounts.tokens,
                        "voice_seconds": bucket["voice_seconds"] + amounts.voice_seconds,
                    },
                )
            )
        for bucket_key, bucket in writes:
            tx.set(bucket_key, bucket)
        tx.set(
            run_path(tenant, run),
            {**active, "reservation_keys": [*active["reservation_keys"], key]},
        )
        tx.create(
            path,
            {
                "tenant_id": tenant,
                "run_id": run,
                "period": period,
                "scopes": active["scopes"],
                "reserved": amounts.model_dump(),
                "charged": amounts.model_dump(),
                "observed": Amounts().model_dump(),
                "known": {name: value == 0 for name, value in amounts.model_dump().items()},
                "state": "started",
            },
        )

    def observe(self, tenant: ResourceId, run: ResourceId, key: str, observed: Amounts) -> None:
        """Persist cumulative lower bounds without declaring the provider finished.

        A partial report never refunds a reservation. Smaller or repeated reports
        do not lower a saved bound or duplicate the charge.
        """
        self._record(tenant, run, key, observed=observed, actual=None)

    def settle(
        self,
        tenant: ResourceId,
        run: ResourceId,
        key: str,
        actual: Settlement,
        *,
        observed: Amounts | None = None,
    ) -> None:
        """Record completion facts and cumulative lower bounds after lease/auth loss.

        Unknown units retain max(reservation, observed). Final totals must cover
        every observed unit. Valid observations are persisted even if a conflicting
        final fact is rejected, so that rejecting a bad fact cannot undercharge.
        """
        self._record(tenant, run, key, observed=observed or Amounts(), actual=actual)

    def _record(
        self,
        tenant: ResourceId,
        run: ResourceId,
        key: str,
        *,
        observed: Amounts,
        actual: Settlement | None,
    ) -> None:
        def work(tx: Transaction) -> bool:
            path = reservation_path(tenant, run, key)
            doc = tx.get(path)
            if doc is None:
                raise ValueError("usage_reservation_missing")
            charged, known = dict(doc["charged"]), dict(doc["known"])
            # Older reservations predate explicit observations; their lower bound
            # is zero, never their reserved (potentially unused) allowance.
            lower = Amounts.model_validate(doc.get("observed", {})).model_dump()
            for unit, value in observed.model_dump().items():
                lower[unit] = max(lower[unit], value)
                if known[unit] and lower[unit] > charged[unit]:
                    # Contradictory later evidence invalidates the final fact. Hold
                    # conservatively until an adequate final total is reconciled.
                    known[unit] = False
                if not known[unit]:
                    charged[unit] = max(charged[unit], doc["reserved"][unit], lower[unit])
            facts = {} if actual is None else actual.model_dump()
            conflict = any(
                value is not None
                and (value < lower[unit] or (doc["known"][unit] and doc["charged"][unit] != value))
                for unit, value in facts.items()
            )
            if not conflict:
                for unit, value in facts.items():
                    if value is not None:
                        charged[unit], known[unit] = value, True
            before, after = Amounts.model_validate(doc["charged"]), Amounts.model_validate(charged)
            writes = []
            for scope in doc["scopes"]:
                bucket_key = bucket_path(tenant, doc["period"], scope)
                bucket = tx.get(bucket_key)
                if bucket is None:
                    raise ValueError("usage_bucket_missing")
                writes.append(
                    (
                        bucket_key,
                        {
                            **bucket,
                            "tokens": bucket["tokens"] + after.tokens - before.tokens,
                            "voice_seconds": bucket["voice_seconds"]
                            + after.voice_seconds
                            - before.voice_seconds,
                        },
                    )
                )
            state = "settled" if all(known.values()) else "unknown"
            if actual is None and doc["state"] == "started":
                state = "started"
            for bucket_key, bucket in writes:
                tx.set(bucket_key, bucket)
            tx.set(
                path,
                {**doc, "charged": charged, "observed": lower, "known": known, "state": state},
            )
            return conflict

        if self.store.run(work):
            raise ValueError("usage_fact_conflict")

    def prepare_release(
        self, tx: Transaction, tenant: ResourceId, run: ResourceId, *, abandon: bool = False
    ) -> Callable[[], None]:
        doc = tx.get(run_path(tenant, run))
        if doc is None or not doc["active"]:
            return lambda: None
        reservations = [
            (reservation_path(tenant, run, key), tx.get(reservation_path(tenant, run, key)))
            for key in doc["reservation_keys"]
        ]
        started = [
            (path, saved)
            for path, saved in reservations
            if saved is not None and saved["state"] == "started"
        ]
        if started and not abandon:
            return lambda: None
        slots = []
        for scope in doc["scopes"]:
            path = f"tenants/{tenant}/usage_slots/{scope}"
            saved = tx.get(path)
            if saved is None or saved["active"] < 1:
                raise ValueError("usage_slot_inconsistent")
            slots.append((path, {"active": saved["active"] - 1}))

        def write() -> None:
            for path, saved in started:
                tx.set(path, {**saved, "state": "unknown"})
            for path, slot in slots:
                tx.set(path, slot)
            tx.set(run_path(tenant, run), {**doc, "active": False})

        return write

    def view(self, tx: Transaction, tenant: ResourceId, *, user: FirebaseUid | None) -> UsageView:
        policy = self.policy(tx, tenant)
        period = normalize_utc(self.now()).strftime("%Y-%m")
        scope = scope_key("tenant") if user is None else scope_key("user", user)
        own = tx.get(bucket_path(tenant, period, scope)) or empty()
        scopes = [(scope, policy.tenant if user is None else policy.user)]
        if user is not None:
            scopes.append((scope_key("tenant"), policy.tenant))
        remaining = []
        limited = False
        exhausted = False
        for item, limit in scopes:
            bucket = own if item == scope else tx.get(bucket_path(tenant, period, item)) or empty()
            slots = tx.get(f"tenants/{tenant}/usage_slots/{item}") or {"active": 0}
            remaining.append(max(0, limit.turns - bucket["turns"]))
            exhausted |= (
                bucket["turns"] >= limit.turns
                or bucket["tokens"] >= limit.tokens
                or bucket["voice_seconds"] >= limit.voice_seconds
            )
            limited |= slots["active"] >= limit.concurrent_runs
        return UsageView(
            period=period,
            turns=own["turns"],
            voice_seconds=own["voice_seconds"],
            remaining_turns=min(remaining),
            quota_state="exhausted" if exhausted else "limited" if limited else "available",
        )
