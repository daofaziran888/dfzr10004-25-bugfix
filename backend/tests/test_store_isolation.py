"""内存仓库回归：读路径返回副本，写路径走 find_live，防止内部状态被外部意外改写。"""
from __future__ import annotations

import unittest

from app.services.throttle import ThrottleService
from app.store import store


class StoreIsolationTest(unittest.TestCase):
    def setUp(self):
        store.__init__()

    def test_find_returns_copy_that_cannot_mutate_store(self):
        snapshot = store.find("throttle", 1)
        self.assertIsNotNone(snapshot)
        snapshot["status"] = "被外部篡改"
        snapshot["日请求上限"] = -999
        live = store.find_live("throttle", 1)
        self.assertNotEqual(live["status"], "被外部篡改")
        self.assertNotEqual(live["日请求上限"], -999)

    def test_find_live_mutation_persists_and_find_reflects_it(self):
        live = store.find_live("throttle", 1)
        live["status"] = "停用"
        self.assertEqual(store.find("throttle", 1)["status"], "停用")

    def test_service_get_entry_does_not_leak_mutable_row(self):
        service = ThrottleService()
        entry = service.get_entry(1)
        entry["status"] = "外部改写"
        again = service.get_entry(1)
        self.assertNotEqual(again["status"], "外部改写")


if __name__ == "__main__":
    unittest.main()
