"""DEV10 Pi composition against real local Auth/Firestore API persistence."""

import asyncio
import tempfile
from pathlib import Path

from brainoid_api.app import create_app
from brainoid_api.settings import load_settings
from brainoid_device.cloud_client import CloudClient
from brainoid_device.cloud_models import CloudError
from brainoid_device.config import CloudSettings
from brainoid_device.ledger import DeviceStorage
from brainoid_device.local_api import Sessions
from brainoid_device.runtime import Runtime
from brainoid_device.storage import CredentialStore
from brainoid_device.ui_bridge import ListConversations, UIBridge
from starlette.testclient import TestClient
from test_api_foundation import EMULATOR_ENV
from test_device_cloud import Proxy
from test_device_pairing_integration import DevicePairingIntegrationFixture


class DeviceCloudIntegrationTests(DevicePairingIntegrationFixture):
    def test_device_exchange_lost_refresh_reopen_current_authorization(self) -> None:
        app = create_app(settings_loader=lambda: load_settings(EMULATOR_ENV))
        with (
            TestClient(app),
            tempfile.TemporaryDirectory(prefix="ba-dev-10-writer-cloud-emulator-") as name,
        ):
            root = Path(name)
            (root / "credentials").mkdir(mode=0o700)
            (root / "state").mkdir(mode=0o700)
            runtime = Runtime()
            store = CredentialStore(root / "credentials")
            storage = DeviceStorage(root / "state", store, cleanup_private=runtime.clear_private)
            proxy = Proxy(app)
            settings = CloudSettings("http://127.0.0.1:18777", local_test=True)

            async def exercise() -> None:
                runtime.running = True
                cloud = CloudClient(settings, runtime, storage, store, transport=proxy)
                try:
                    await cloud.begin_pairing("Emulator Pi")
                    pairing = cloud.journal.pairing
                    assert pairing is not None
                    self.confirm(pairing)  # type: ignore[arg-type]
                    self.assertTrue(await cloud.exchange())
                    credentials = cloud.journal.credentials
                    assert credentials is not None
                    path = f"tenants/{self.tenant}/devices/{credentials.device_id}"
                    self.db.document(path).update({"capabilities": ["display"]})
                    proxy.lose, proxy.release = "device-tokens:refresh", asyncio.Event()
                    task = asyncio.create_task(cloud.token(rejected=await cloud.token()))
                    await asyncio.wait_for(proxy.entered.wait(), 10)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    original = proxy.requests[-1]
                    await cloud.close()
                    proxy.release = None
                    cloud = CloudClient(settings, runtime, storage, store, transport=proxy)
                    await cloud.token()
                    self.assertEqual(proxy.requests[-1], original)
                    bridge = UIBridge(cloud, Sessions())
                    result = await bridge.operation(
                        ListConversations(operation="list_conversations")
                    )
                    self.assertTrue(result["items"])
                    self.assertTrue(all(row["visibility"] == "shared" for row in result["items"]))
                    self.db.document(path).update({"status": "revoked"})
                    with self.assertRaises(CloudError) as denied:
                        await bridge.operation(ListConversations(operation="list_conversations"))
                    self.assertEqual(
                        (denied.exception.code, denied.exception.status),
                        ("credential_refused", 401),
                    )
                    self.assertFalse(cloud.active)
                    self.assertIsNone(cloud.journal.credentials)
                finally:
                    await cloud.close()
                    await runtime.close()

            try:
                asyncio.run(exercise())
            finally:
                storage.close()
                store.close()
