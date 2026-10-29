"""Schema-bound lifecycle selection for the shared bounded postorder walker."""

from brainoid_contracts.access import fingerprint
from brainoid_contracts.deletion import subject_id
from brainoid_contracts.transactions import Document, Transaction
from brainoid_memory.deletion_ledger import DeletionIntent
from brainoid_memory.push_installations import ACCOUNT_GLOBALS

from brainoid_worker.purge_handlers import VECTOR, Decision, ForgetHandlers, LegacyProvenanceMissing

# Persistent restoration/worker fences contain no business text. Keep accepted
# erasure provenance and active purge envelopes until all overlapping purges finish.
CONTROL = frozenset(
    {
        "operations",
        "deletion_tombstones",
        "membership_deletion_fences",
        "memory_forget_reservations",
        "note_delete_reservations",
        "conversation_delete_reservations",
        "task_delete_reservations",
        "source_forget_reservations",
        "memory_forget_effects",
        "purge_barriers",
        "lifecycle_selections",
        "integration_deletion_fences",
        "calendar_disconnect_requests",
        "calendar_export_barriers",
    }
)
PRIVATE = frozenset(
    {"conversations", "facts", "entities", "notes", "tasks", "reminders", "commands"}
)
PERSONAL = frozenset(
    {
        "memberships",
        "private_sessions",
        "integrations",
        "calendar_authorizations",
        "calendar_proposals",
        "notifications",
        "reminder_schedules",
        "exports",
        "caches",
        "audio_inputs",
        "speech_outputs",
    }
)
# Closed inventory of local producers. Global discovery is available only to a
# whole-tenant intent; membership/account deletion does not own tenant devices.
DEVICE_GLOBALS = (
    "device_credential_families",
    "device_pairings",
    "device_installations",
    "device_pairing_requests",
    "device_pairing_selectors",
    "device_refresh_tokens",
)


class ProfileParentUnknown(LegacyProvenanceMissing):
    """A live/ghost profile without canonical UID cannot be assigned to this account."""


class LifecycleHandlers(ForgetHandlers):
    def root(self, name: str, intent: DeletionIntent | None = None) -> bool:
        if intent is not None and intent.kind == "disconnect_calendar":
            from brainoid_worker.calendar_erasure import ROOTS

            return name in ROOTS or VECTOR.fullmatch(name) is not None
        return True

    @staticmethod
    def selection_path(intent: DeletionIntent, path: str) -> str:
        return f"tenants/{intent.tenant_id}/lifecycle_selections/{fingerprint([intent.operation_id, path])}"

    def selected(
        self, tx: Transaction, path: str, doc: Document | None, intent: DeletionIntent
    ) -> bool:
        if intent.kind == "disconnect_calendar":
            from brainoid_worker.calendar_erasure import selected

            return selected(tx, path, doc, intent)
        parts = path.split("/")
        if parts[0] in DEVICE_GLOBALS:
            if len(parts) != 2 or intent.kind != "delete_tenant" or doc is None:
                return False
            if parts[0] in {
                "device_credential_families",
                "device_pairings",
                "device_installations",
            }:
                if parts[0] == "device_pairings" and doc.get("tenant_id") is None:
                    # A pending re-pair already belongs to a tenant when its
                    # installation was bound by an earlier confirmed pairing.
                    installation = doc.get("installation")
                    return (
                        isinstance(installation, str)
                        and "/" not in installation
                        and self.bound_selection(tx, f"device_installations/{installation}", intent)
                    )
                return doc.get("tenant_id") == str(intent.scopes[0].tenant_id)
            binding = (
                ("device_credential_families", "family_id")
                if parts[0] == "device_refresh_tokens"
                else ("device_pairings", "pairing_id")
            )
            identity = doc.get(binding[1])
            return (
                isinstance(identity, str)
                and "/" not in identity
                and self.bound_selection(tx, f"{binding[0]}/{identity}", intent)
            )
        if parts[0] in ACCOUNT_GLOBALS:
            return (
                len(parts) == 2
                and intent.kind == "delete_account"
                and doc is not None
                and isinstance(doc.get("user_id"), str)
                and subject_id(doc["user_id"]) == intent.scopes[0].resource_id
            )
        if parts[0] == "users":
            return (
                len(parts) == 2
                and intent.kind == "delete_account"
                and doc is not None
                and isinstance(doc.get("user_id"), str)
                and subject_id(doc["user_id"]) == intent.scopes[0].resource_id
            )
        if len(parts) != 4:
            return False
        tenant, root = parts[1], parts[2]
        scope = next((s for s in intent.scopes if str(s.tenant_id) == tenant), None)
        if scope is None or root in CONTROL:
            return False
        if doc is None:
            return scope.kind == "tenant"
        if doc.get("tenant_id", tenant) != tenant:
            raise ValueError("Invalid lifecycle tenant binding")
        if root == "jobs" and doc.get("kind") in {"memory_purge", "lifecycle_purge"}:
            return False
        if scope.kind == "tenant":
            return True
        if root in PRIVATE:
            owner = doc.get("owner_user_id")
            return (
                doc.get("visibility") == "private"
                and isinstance(owner, str)
                and subject_id(owner) == scope.resource_id
            )
        if root in PERSONAL:
            if root == "integrations" and doc.get("visibility") != "private":
                if (
                    doc.get("visibility") != "shared"
                    and isinstance(doc.get("owner_user_id"), str)
                    and subject_id(doc["owner_user_id"]) == scope.resource_id
                ):
                    raise LegacyProvenanceMissing()
                return False
            owner = doc.get("user_id", doc.get("owner_user_id"))
            return isinstance(owner, str) and subject_id(owner) == scope.resource_id
        if VECTOR.fullmatch(root):
            canonical = f"tenants/{tenant}/facts/{parts[3]}"
            return self.bound_selection(tx, canonical, intent)
        if root == "entity_external_ids":
            entity = doc.get("entity_id")
            return (
                isinstance(entity, str)
                and "/" not in entity
                and self.bound_selection(tx, f"tenants/{tenant}/entities/{entity}", intent)
            )
        if root == "home_targets":
            entity = doc.get("entity_id")
            return (
                isinstance(entity, str)
                and "/" not in entity
                and self.bound_selection(tx, f"tenants/{tenant}/entities/{entity}", intent)
            )
        if root == "runs":
            parent = doc.get("conversation_id")
            return isinstance(parent, str) and self.bound_selection(
                tx, f"tenants/{tenant}/conversations/{parent}", intent
            )
        if root == "jobs":
            ref = doc.get("grant", {}).get("resource", {}).get("ref", {})
            if ref.get("tenant_id") != tenant or ref.get("kind") not in PRIVATE | {"notifications"}:
                return False
            return self.bound_selection(
                tx, f"tenants/{tenant}/{ref['kind']}/{ref.get('id')}", intent
            )
        if root in {"tool_executions", "run_proposals"}:
            run = doc.get("run_id", doc.get("authority", {}).get("run_id"))
            return isinstance(run, str) and self.bound_selection(
                tx, f"tenants/{tenant}/runs/{run}", intent
            )
        if root == "task_events":
            return (
                doc.get("visibility") == "private"
                and isinstance(doc.get("owner_user_id"), str)
                and subject_id(doc["owner_user_id"]) == scope.resource_id
            )
        if root in {"memory_effects", "note_effects", "task_effects", "reminder_effects"}:
            return any(
                isinstance(record, dict)
                and record.get("visibility") == "private"
                and isinstance(record.get("owner_user_id"), str)
                and subject_id(record["owner_user_id"]) == scope.resource_id
                for record in (doc.get("record"), doc.get("previous"))
            )
        if root == "private_session_slots":
            session = doc.get("session_id")
            return isinstance(session, str) and self.bound_selection(
                tx, f"tenants/{tenant}/private_sessions/{session}", intent
            )
        if root == "private_session_requests":
            session = doc.get("session_id", doc.get("response", {}).get("session_id"))
            return isinstance(session, str) and self.bound_selection(
                tx, f"tenants/{tenant}/private_sessions/{session}", intent
            )
        return False

    def bound_selection(self, tx: Transaction, path: str, intent: DeletionIntent) -> bool:
        doc = tx.get(path)
        if doc is None and self.selected(tx, path, None, intent):
            return True
        if doc is not None:
            return self.selected(tx, path, doc, intent)
        return tx.get(self.selection_path(intent, path)) == {
            "operation_id": intent.operation_id,
            "path": path,
            "request_hash": intent.request_hash,
        }

    def bound(self, tx: Transaction, path: str, intent: DeletionIntent, force: bool) -> bool:
        if intent.kind == "disconnect_calendar":
            from brainoid_worker.calendar_erasure import bound

            return bound(tx, path, intent, force)
        parts = path.split("/")
        if parts[0] in DEVICE_GLOBALS:
            if intent.kind != "delete_tenant":
                raise ValueError("Unaccepted device credential scope")
            if len(parts) == 1:
                if force:
                    raise ValueError("Global discovery cannot inherit deletion authority")
                return True
            selected = self.bound_selection(tx, "/".join(parts[:2]), intent)
            if force and not selected:
                raise ValueError("Changed device credential scope")
            return selected if len(parts) > 2 else True
        if parts[0] in ACCOUNT_GLOBALS:
            if intent.kind != "delete_account" or (len(parts) == 1 and force):
                raise ValueError("Unaccepted global push scope")
            if len(parts) <= 2:
                return True
            selected = self.bound_selection(tx, "/".join(parts[:2]), intent)
            if force and not selected:
                raise ValueError("Changed push ownership")
            return selected
        if parts[0] == "users":
            if intent.kind != "delete_account":
                raise ValueError("Unaccepted global scope")
            if len(parts) == 1:
                if force:
                    raise ValueError("Global discovery cannot inherit deletion authority")
                return True
            ancestor = "/".join(parts[:2])
            selected = self.bound_selection(tx, ancestor, intent)
            if force and not selected:
                raise ValueError("Changed profile scope")
            return selected if len(parts) > 2 else True
        scope = next((s for s in intent.scopes if str(s.tenant_id) == parts[1]), None)
        if parts[0] != "tenants" or scope is None:
            raise ValueError("Unaccepted tenant cursor")
        if len(parts) <= 3:
            return True
        root = parts[2]
        if root in CONTROL:
            if force or len(parts) > 4:
                raise ValueError("Control data is not an erasure target")
            return True
        ancestor = "/".join(parts[:4])
        if scope.kind == "tenant":
            doc = tx.get(ancestor)
            if (
                root == "jobs"
                and doc is not None
                and doc.get("kind") in {"memory_purge", "lifecycle_purge"}
            ):
                if force or len(parts) > 4:
                    raise ValueError("Purge authority must survive")
            return True
        selected = self.bound_selection(tx, ancestor, intent)
        if force and not selected:
            raise ValueError("Changed private scope")
        return selected if len(parts) > 4 else True

    def decide(
        self,
        tx: Transaction,
        path: str,
        doc: Document | None,
        intent: DeletionIntent,
        cutoffs: Document,
    ) -> Decision:
        if intent.kind == "disconnect_calendar":
            from brainoid_worker.calendar_erasure import decide

            return decide(tx, path, doc, intent)
        parts = path.split("/")
        if parts[0] in DEVICE_GLOBALS and len(parts) == 2 and doc is None:
            if not self.bound_selection(tx, path, intent):
                raise LegacyProvenanceMissing()
        if parts[0] == "users" and len(parts) == 2:
            if doc is None:
                if self.bound_selection(tx, path, intent):
                    return Decision(children=True, selected=True)
                # show_missing returned a ghost parent with descendants. No UID
                # mapping means neither erasure nor an empty-account certificate.
                raise ProfileParentUnknown()
            if not isinstance(doc.get("user_id"), str):
                raise ProfileParentUnknown()
        if len(parts) == 2 and parts[0] == "tenants":
            return Decision(children=True)
        if parts[0] == "tenants" and parts[2] in CONTROL:
            return Decision()
        selected = self.selected(tx, path, doc, intent)
        if (
            selected
            and doc is not None
            and (
                parts[0] in DEVICE_GLOBALS
                or (len(parts) == 4 and parts[2] == "entity_external_ids")
            )
        ):
            if parts[0] == "device_pairing_selectors":
                return Decision(erase=True, children=True)
            fields, status = {
                "device_credential_families": ({"tenant_id", "device_id"}, "revoked"),
                "device_pairings": ({"tenant_id", "device_id"}, "expired"),
                "device_installations": (
                    {"tenant_id", "device_id", "original_device_id"},
                    "revoked",
                ),
                "device_pairing_requests": ({"pairing_id"}, "expired"),
                "device_refresh_tokens": ({"family_id"}, "revoked"),
                "entity_external_ids": ({"entity_id", "deletion_epoch"}, "deleted"),
            }[parts[0] if parts[0] in DEVICE_GLOBALS else parts[2]]
            retained = {k: v for k, v in doc.items() if k in fields}
            if parts[0] == "device_pairings":
                retained["tenant_id"] = str(intent.scopes[0].tenant_id)
            retained["status"] = status
            return Decision(
                replacement=retained if retained != doc else None, children=True, selected=True
            )
        if (
            selected
            and intent.kind == "leave_tenant"
            and len(parts) == 4
            and parts[2] == "memberships"
            and doc is not None
        ):
            retained = {
                k: v
                for k, v in doc.items()
                if k
                in {
                    "tenant_id",
                    "user_id",
                    "role",
                    "status",
                    "authorization_version",
                    "authorization_operation_id",
                    "authorization_change",
                }
            }
            return Decision(
                replacement=retained if retained != doc else None, children=True, selected=True
            )
        if doc is None:
            # Traverse ghost roots but do not guess ownership of surviving children.
            selected = self.bound_selection(tx, path, intent)
            return Decision(children=True, selected=selected)
        return Decision(erase=selected, children=selected)

    def record_selection(self, tx: Transaction, path: str, intent: DeletionIntent) -> None:
        location = self.selection_path(intent, path)
        # Read before writes: caller has already evaluated all scope decisions.
        tx.set(
            location,
            {
                "operation_id": intent.operation_id,
                "path": path,
                "request_hash": intent.request_hash,
            },
        )
        if "/memberships/" not in path and path.startswith("tenants/"):
            tx.set(
                f"resource_deletion_fences/{fingerprint(path)}",
                {"operation_id": intent.operation_id},
            )
