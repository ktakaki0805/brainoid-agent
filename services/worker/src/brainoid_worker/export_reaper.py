"""Delete-only maintenance independent of an exporter's revoked ordinary Jobs grant."""

from brainoid_contracts.transactions import Transaction
from brainoid_memory.exports import Exports


class ExportReaper:
    def __init__(self, exports: Exports) -> None:
        self.exports = exports

    def step(self) -> str:
        """Call periodically from the explicitly composed trusted scheduler.

        Each call inspects at most 50 durable registry entries. The cursor survives
        process restart; replay after a crash is safe. Every completed pass restarts
        at the beginning, so expiration and scope changes need no download request.
        """
        path = "export_maintenance/reaper"
        saved = self.exports.store.run(lambda tx: tx.get(path))
        token = "" if saved is None else saved["cursor"]
        next_token = self.exports.sweep(token=token, limit=50)

        def save(tx: Transaction) -> None:
            tx.set(path, {"cursor": next_token, "updated_at": self.exports.now().isoformat()})

        self.exports.store.run(save)
        return next_token
