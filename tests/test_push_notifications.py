"""APP-05 / T-030 T-038 HTTP and delivery tests with strict transaction fake."""

from fakes import push_scenarios
from test_notifications import NotificationStore


class PushTests(push_scenarios.PushScenarios):
    def setUp(self) -> None:
        self.store = NotificationStore()
        self.setup_push()
