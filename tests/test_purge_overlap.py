"""R3: an overlapping purge can remove a selected ancestor during a pause.

Only the nested copies are synthetic subtree-contract fixtures. The receipt,
both forget acceptances, walker cursor and recovery use the canonical producers.
"""

import copy
from typing import Any

import test_purge_cross_forget
from brainoid_jobs import LeaseLost, RetryableError
from brainoid_worker.entry import Worker
from brainoid_worker.local import LocalCaller, LocalQueue
from brainoid_worker.purge_resource import PurgeDispatcher


class OverlapFixture(test_purge_cross_forget.CrossForgetFixture):
    def paused_overlap(self, *, collection: bool, receipt_first: bool = False) -> dict[str, Any]:
        case = self.prepare(searched=True)
        receipt = next(
            p
            for p, d in self.store.data.items()
            if "/tool_executions/" in p and d.get("name") == "memory.search"
        )
        child = receipt + "/copies/part"
        self.store.data[child] = {"body": "SYNTHETIC_NESTED_COPY"}
        self.accept_purge(case["a"].id, case["ledger"])
        if receipt_first:
            self.purge.pages = test_purge_cross_forget.ReceiptFirstPages(self.pages.store)
        cursor = receipt + "/copies" if collection else child
        for _ in range(3000):
            self.purge.step(self.purge_execution)
            top = self.store.data[self.op_path]["purge"]["stack"][-1]
            if top["path"] == cursor:
                break
        else:
            self.fail("Normal walker did not reach selected nested copy")
        self.assertTrue(top["force"])
        self.assertIn(receipt, self.store.data)
        a = self.purge, self.purge_jobs, self.purge_execution, self.purge_id, self.op_path
        self.bind_purge(case["ledger"], case["b_operation"])
        self.finish_purge()
        self.assertFalse(any(p.startswith(receipt + "/") or p == receipt for p in self.store.data))
        self.purge, self.purge_jobs, self.purge_execution, self.purge_id, self.op_path = a
        return {**case, "receipt": receipt, "child": child, "cursor": cursor}

    def recover_normally(self) -> None:
        queue = LocalQueue()
        dispatcher = PurgeDispatcher(self.purge, self.pages, queue)
        worker = Worker(self.purge_jobs, LocalCaller(), {"memory_purge": self.purge})
        old = self.purge_execution
        self.clock.advance(120)
        dispatcher.recover(self.tenant)
        with self.assertRaises(LeaseLost):
            self.purge.step(old)
        queue.drain(worker)


class PurgeOverlapTests(OverlapFixture):
    def test_empty_document_and_collection_cursors_resume_in_both_page_orders(self) -> None:
        # Fresh canonical fixture per variant; no shared mutable state between cases.
        for collection, order in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(collection=collection, receipt_first=order):
                if collection or order:
                    self.tearDown()
                    self.setUp()
                case = self.paused_overlap(collection=collection, receipt_first=order)
                before = self.store.data[self.op_path]["purge"]["processed"]
                self.purge.step(self.purge_execution)
                self.assertEqual(self.store.data[self.op_path]["purge"]["processed"], before)
                self.recover_normally()
                saved = self.store.data[self.op_path]
                self.assertEqual(saved["operation"]["status"], "succeeded")
                self.assertNotIn(f"tenants/{self.tenant}/facts/{case['a'].id}", self.store.data)
                # The original and any unvisited legitimate root shrink count;
                # the empty branch itself and subsequent redelivery never count.
                self.assertGreater(saved["purge"]["processed"], before)
                count = saved["purge"]["processed"]
                self.recover_normally()
                self.assertEqual(self.store.data[self.op_path]["purge"]["processed"], count)
                self.assertFalse(any(p.startswith(case["receipt"]) for p in self.store.data))

    def test_missing_ancestor_does_not_authorize_a_remaining_child(self) -> None:
        case = self.paused_overlap(collection=False)
        # A missing canonical ancestor is not authority over any new/unknown copy.
        self.store.data[case["child"]] = {
            "tenant_id": "00000000-0000-0000-0000-000000000099",
            "memory_id": case["unrelated_id"],
            "body": "UNPROVEN_PRIVATE_COPY",
        }
        snapshot = copy.deepcopy(self.store.data[case["child"]])
        before = self.store.data[self.op_path]["purge"]["processed"]
        self.recover_normally()
        saved = self.store.data[self.op_path]
        self.assertNotEqual(saved["operation"]["status"], "succeeded")
        self.assertEqual(saved["purge"]["retry_reason"], "derived_ancestor_missing")
        self.assertEqual(saved["purge"]["processed"], before)
        self.assertEqual(self.store.data[case["child"]], snapshot)
        self.assertIn(f"tenants/{self.tenant}/facts/{case['a'].id}", self.store.data)

    def test_zero_change_pass_revisits_ghost_ancestor_instead_of_skipping_unknown_sibling(
        self,
    ) -> None:
        case = self.paused_overlap(collection=False)
        # Let the stale document cursor finish empty, then add an unproven sibling
        # behind the old page position. Full verification must revisit its ghost root.
        for _ in range(4):
            self.purge.step(self.purge_execution)
        unknown = case["receipt"] + "/earlier/missing/parts/remaining"
        self.store.data[unknown] = {"body": "UNPROVEN_REMAINING_COPY"}
        with self.assertRaises(RetryableError):
            self.purge.run(self.purge_execution)
        self.assertIn(unknown, self.store.data)
        self.assertEqual(
            self.store.data[self.op_path]["purge"]["retry_reason"], "derived_ancestor_missing"
        )
        self.assertFalse(self.store.data[self.op_path]["purge"]["verified"])

    def test_absent_ancestor_does_not_relax_tenant_or_original_target_cursor_fences(self) -> None:
        case = self.paused_overlap(collection=False)
        original = copy.deepcopy(self.store.data[self.op_path]["purge"]["stack"])
        for path in (
            case["cursor"].replace(self.tenant, "00000000-0000-0000-0000-000000000099"),
            f"tenants/{self.tenant}/facts/{case['unrelated_id']}/copies/missing",
        ):
            with self.subTest(path=path):
                self.store.data[self.op_path]["purge"]["stack"][-1]["path"] = path
                with self.assertRaises(ValueError):
                    self.purge.step(self.purge_execution)
                self.store.data[self.op_path]["purge"]["stack"] = copy.deepcopy(original)
        self.assertIn(case["unrelated_path"], self.store.data)
