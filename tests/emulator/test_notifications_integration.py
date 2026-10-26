"""APP-04 same notification recovery scenarios using real Firestore transactions."""

import os

from brainoid_persistence.firestore import FirestoreTransactions
from fakes import notification_scenarios
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from scripts.demo_emulator import DATABASE, PROJECT, DemoEmulator


class NotificationIntegrationTests(notification_scenarios.NotificationScenarios):
    def setUp(self) -> None:
        emulator = DemoEmulator(os.environ)
        emulator.reset()
        self.addCleanup(emulator.reset)
        db = Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())  # type: ignore[no-untyped-call]
        self.addCleanup(db.close)
        self.store = FirestoreTransactions(db)
        self.setup_notifications()
