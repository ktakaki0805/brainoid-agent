"""DEV07 extension is readable/prunable/resettable by the shipped schema-2 code."""

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from brainoid_device.command_journal import CommandJournal
from test_device_storage import StorageFixture

BASE = "92b2acb4952f321c4eccd36159223f12717542dc"
ROOT = Path(__file__).resolve().parents[1]


class CommandUpgradeTests(StorageFixture):
    def test_old_schema2_daemon_updater_prune_and_reset_preserve_new_report_safety(self) -> None:
        storage, delivery = self.seed()
        journal = CommandJournal(storage)
        journal.prepare(delivery.epoch)
        payload: dict[str, Any] = {
            "tenant_id": delivery.tenant_id,
            "device_id": delivery.device_id,
            "command_id": str(uuid4()),
            "delivery_seq": 1,
            "target_entity_id": str(uuid4()),
            "operation": {"action": "light.turn_on", "parameters": {}},
            "created_at": "1970-01-01T00:00:00+00:00",
            "expires_at": "1970-01-01T00:00:30+00:00",
        }
        value = journal.receive(
            delivery.epoch, 1, payload, now_ms=0, observed_at=payload["created_at"]
        )
        storage.record_started(
            delivery.epoch,
            value.command.command_id,
            value.command.execution_id,
            1,
            grant_expires_at_ms=30000,
            now_ms=1,
            clock_trusted=True,
        )
        journal.accepted(delivery.epoch, value.command.command_id)
        report = {
            "command_id": value.command.command_id,
            "execution_id": value.command.execution_id,
            "status": "succeeded",
            "observed_at": "1970-01-01T00:00:01+00:00",
            "observed_state": {"power": "on"},
        }
        journal.finish(delivery.epoch, report, now_ms=1000)
        journal.clock(delivery.epoch, 1000)
        storage.close()
        old = self.root / "old"
        old.mkdir()
        archive = subprocess.run(
            ["git", "archive", BASE, "services/device/src/brainoid_device"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=10,
        )
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as source:
            source.extractall(old, filter="data")
        probe = """
import json,sys
from pathlib import Path
from typing import Any
sys.path.insert(0, sys.argv[1])
from brainoid_device.ledger import DeviceStorage
from brainoid_device.storage import CredentialStore
from brainoid_device.update_runtime import check_storage
check_storage(Path(sys.argv[2]),Path(sys.argv[3]))
store=CredentialStore(Path(sys.argv[3]));db=DeviceStorage(Path(sys.argv[2]),store,cleanup_private=lambda:None)
try:
 assert db.snapshot().status=='ready'
 assert db._db.execute('PRAGMA user_version').fetchone()[0]==2
 if sys.argv[4]=='inspect':
  row=db._db.execute('SELECT reports FROM command_execution_v1').fetchone();print(row[0])
 elif sys.argv[4]=='prune':
  assert db.prune_commands(now_ms=8*86400*1000,clock_trusted=True)==1
  assert db._db.execute('SELECT count(*) FROM command_execution_v1').fetchone()[0]==0
 else:
  assert db.local_reset()
  for name in ('command_execution_v1','command_clock_v1'):
   assert db._db.execute('SELECT count(*) FROM '+name).fetchone()[0]==0
finally:db.close();store.close()
"""

        def run(mode: str) -> subprocess.CompletedProcess[str]:
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    probe,
                    str(old / "services/device/src"),
                    str(self.state),
                    str(self.private),
                    mode,
                ],
                text=True,
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result

        self.assertEqual(json.loads(run("inspect").stdout)["succeeded"], report)
        reopened = self.storage()
        journal = CommandJournal(reopened)
        self.assertEqual(
            journal.get(delivery.epoch, value.command.command_id).reports["succeeded"], report
        )
        journal.acknowledge(delivery.epoch, journal.get(delivery.epoch, value.command.command_id))
        reopened.close()
        run("prune")
        reopened = self.storage()
        journal = CommandJournal(reopened)
        payload.update(command_id=str(uuid4()), delivery_seq=2)
        journal.receive(delivery.epoch, 1, payload, now_ms=0, observed_at=payload["created_at"])
        reopened.close()
        run("reset")
