"""路由层（HTTP 契约）回归测试：直接调用路由函数，不引入额外依赖。

覆盖：站点目录、保存拒绝口径、动作幂等、冲突拒绝带策略编号、
停用联动待重新排队、计划排队取数、列表与详情数量一致。
"""
from __future__ import annotations

import unittest

from app.routers import taskplan as taskplan_router
from app.routers import throttle as throttle_router
from app.schemas import EntryPayload
from app.store import store


def payload(**values):
    return EntryPayload(values=values)


class ThrottleApiTest(unittest.TestCase):
    def setUp(self):
        store.__init__()

    def test_sites_endpoint_lists_catalog(self):
        result = throttle_router.list_sites()
        names = [item["目标站点"] for item in result["items"]]
        self.assertIn("新闻资讯站A", names)
        self.assertEqual(len(names), 4)

    def test_create_rejects_negative_interval_via_api(self):
        result = throttle_router.create_entry(payload(
            策略编号="THRO-API1", 目标站点="新闻资讯站A",
            请求间隔=-5, 日请求上限=100, 并发数量=1, 优先级权重=10,
        ))
        self.assertFalse(result.ok)
        self.assertIn("请求间隔", result.message)

    def test_create_rejects_over_quota_daily_limit_via_api(self):
        result = throttle_router.create_entry(payload(
            策略编号="THRO-API2", 目标站点="论坛社区站C",
            请求间隔=1000, 日请求上限=99999, 并发数量=1, 优先级权重=10,
        ))
        self.assertFalse(result.ok)
        self.assertIn("日配额", result.message)

    def test_full_conflict_and_replace_flow_via_api(self):
        # 第一条：登记 → 校验 → 启用
        first = throttle_router.create_entry(payload(
            策略编号="THRO-X1", 目标站点="新闻资讯站A",
            请求间隔=300, 日请求上限=1000, 并发数量=2, 优先级权重=10,
        ))
        self.assertTrue(first.ok)
        throttle_router.run_action(first.entry["id"], payload(action="提交校验"))
        throttle_router.run_action(first.entry["id"], payload(action="启用策略"))

        # 第二条同站点策略走完校验后启用：必须被拒绝并指出现有编号
        second = throttle_router.create_entry(payload(
            策略编号="THRO-X2", 目标站点="新闻资讯站A",
            请求间隔=400, 日请求上限=2000, 并发数量=3, 优先级权重=20,
        ))
        throttle_router.run_action(second.entry["id"], payload(action="提交校验"))
        rejected = throttle_router.run_action(second.entry["id"], payload(action="启用策略"))
        self.assertFalse(rejected.ok)
        self.assertIn("THRO-X1", rejected.message)

        # 显式指定以新候选为准
        replaced = throttle_router.run_action(
            second.entry["id"], payload(action="启用策略", replace_policy=True)
        )
        self.assertTrue(replaced.ok)
        self.assertEqual(replaced.entry["status"], "生效")
        old = throttle_router.get_entry(first.entry["id"])
        self.assertEqual(old["status"], "停用")

        # 全站点同时只有一条生效
        listing, _ = throttle_router.service.list_entries(status="生效")
        site_active = [r for r in listing if r["目标站点"] == "新闻资讯站A"]
        self.assertEqual(len(site_active), 1)
        self.assertEqual(site_active[0]["策略编号"], "THRO-X2")

    def test_duplicate_enable_is_idempotent_via_api(self):
        created = throttle_router.create_entry(payload(
            策略编号="THRO-IDEM", 目标站点="新闻资讯站A",
            请求间隔=300, 日请求上限=1000, 并发数量=2, 优先级权重=10,
        ))
        throttle_router.run_action(created.entry["id"], payload(action="提交校验"))
        first = throttle_router.run_action(created.entry["id"], payload(action="启用策略"))
        second = throttle_router.run_action(created.entry["id"], payload(action="启用策略"))
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertIn("未重复推进", second.message)
        self.assertEqual(first.entry["status"], "生效")

    def test_put_update_keeps_old_policy_instead_of_losing_it(self):
        created = throttle_router.create_entry(payload(
            策略编号="THRO-UPD", 目标站点="新闻资讯站A",
            请求间隔=300, 日请求上限=1000, 并发数量=2, 优先级权重=10,
        ))
        before_total = throttle_router.list_entries(keyword=None, status=None, site=None).total
        updated = throttle_router.update_entry(created.entry["id"], payload(日请求上限=1500))
        self.assertTrue(updated.ok)
        self.assertEqual(updated.entry["日请求上限"], 1500)
        # 修改不新增记录，旧策略仍在
        self.assertEqual(
            throttle_router.list_entries(keyword=None, status=None, site=None).total,
            before_total,
        )
        self.assertIsNotNone(throttle_router.get_entry(created.entry["id"]))

    def test_list_and_detail_counts_are_consistent(self):
        for idx in range(3):
            ok = throttle_router.create_entry(payload(
                策略编号=f"THRO-L{idx}", 目标站点=("新闻资讯站A", "电商商品站B", "论坛社区站C")[idx],
                请求间隔=(300, 600, 1200)[idx], 日请求上限=100, 并发数量=1, 优先级权重=10,
            ))
            self.assertTrue(ok.ok)
        page = throttle_router.list_entries(keyword=None, status=None, site=None, page=1, size=200)
        for row in page.items:
            detail = throttle_router.get_entry(int(row["id"]))
            self.assertEqual(detail["策略编号"], row["策略编号"])
        # 导出与列表口径一致
        exported = throttle_router.export_entries()
        self.assertEqual(exported["total"], page.total)
        self.assertEqual(len(exported["items"]), page.total)


class ThrottleTaskplanIntegrationTest(unittest.TestCase):
    def setUp(self):
        store.__init__()

    def _enable_policy(self, **overrides):
        values = {
            "策略编号": "THRO-FLOW", "目标站点": "政务公开站D",
            "请求间隔": 2000, "日请求上限": 1000, "并发数量": 1, "优先级权重": 5,
        }
        values.update(overrides)
        created = throttle_router.create_entry(payload(**values))
        self.assertTrue(created.ok)
        throttle_router.run_action(created.entry["id"], payload(action="提交校验"))
        throttle_router.run_action(created.entry["id"], payload(action="启用策略"))
        return created.entry

    def test_plan_start_blocked_without_active_policy(self):
        plan = taskplan_router.create_entry(payload(
            计划编号="TASK-F1", 目标站点="政务公开站D", 采集频率="每日一轮",
        ))
        self.assertTrue(plan.ok)
        result = taskplan_router.run_action(plan.entry["id"], payload(action="启动计划"))
        self.assertFalse(result.ok)
        self.assertIn("没有生效中的频率策略", result.message)

    def test_disable_policy_pushes_plan_to_requeue_queue(self):
        policy = self._enable_policy()
        plan = taskplan_router.create_entry(payload(
            计划编号="TASK-F2", 目标站点="政务公开站D", 采集频率="每日一轮",
        ))
        started = taskplan_router.run_action(plan.entry["id"], payload(action="启动计划"))
        self.assertTrue(started.ok)
        self.assertIn("THRO-FLOW", started.entry["资源配额"])

        # 停用策略：路由层联动计划转待重新排队
        disabled = throttle_router.run_action(policy["id"], payload(action="停用策略"))
        self.assertTrue(disabled.ok)
        self.assertIn("待重新排队", disabled.message)

        queue = taskplan_router.list_requeue()
        self.assertEqual(queue["total"], 1)
        self.assertEqual(queue["items"][0]["计划编号"], "TASK-F2")

        # 另一侧不能再按已停用规则排队
        blocked = taskplan_router.run_action(plan.entry["id"], payload(action="重新排队"))
        self.assertFalse(blocked.ok)

        # 新策略生效后重新排队成功，额度换成新策略
        new_policy = self._enable_policy(策略编号="THRO-FLOW2", 请求间隔=3000, 日请求上限=500)
        requeued = taskplan_router.run_action(plan.entry["id"], payload(action="重新排队"))
        self.assertTrue(requeued.ok)
        self.assertEqual(requeued.entry["throttle_id"], new_policy["id"])
        self.assertEqual(taskplan_router.list_requeue()["total"], 0)

    def test_replace_policy_evicts_plans_using_old_quota(self):
        # 计划挂在第一条生效策略上
        first = self._enable_policy(策略编号="THRO-R1")
        plan = taskplan_router.create_entry(payload(
            计划编号="TASK-R1", 目标站点="政务公开站D", 采集频率="每日一轮",
        ))
        started = taskplan_router.run_action(plan.entry["id"], payload(action="启动计划"))
        self.assertTrue(started.ok)
        self.assertEqual(started.entry["throttle_id"], first["id"])

        # 第二条候选以 replace_policy 启用，旧策略被隐式停用
        second = throttle_router.create_entry(payload(
            策略编号="THRO-R2", 目标站点="政务公开站D",
            请求间隔=2500, 日请求上限=800, 并发数量=2, 优先级权重=6,
        ))
        throttle_router.run_action(second.entry["id"], payload(action="提交校验"))
        replaced = throttle_router.run_action(
            second.entry["id"], payload(action="启用策略", replace_policy=True)
        )
        self.assertTrue(replaced.ok)
        self.assertIn("待重新排队", replaced.message)
        # 引用旧额度的计划进入排队待办，不能继续按已停用规则跑
        queue = taskplan_router.list_requeue()
        self.assertEqual(queue["total"], 1)
        self.assertEqual(queue["items"][0]["throttle_id"], first["id"])

    def test_updating_active_policy_evicts_plans_using_its_quota(self):
        active = self._enable_policy(策略编号="THRO-U1")
        plan = taskplan_router.create_entry(payload(
            计划编号="TASK-U1", 目标站点="政务公开站D", 采集频率="每日一轮",
        ))
        self.assertTrue(taskplan_router.run_action(
            plan.entry["id"], payload(action="启动计划")
        ).ok)

        # 修改生效策略 → 退回待配置，额度失效，计划转待重新排队
        updated = throttle_router.update_entry(
            active["id"], payload(日请求上限=900)
        )
        self.assertTrue(updated.ok)
        self.assertEqual(updated.entry["status"], "待配置")
        self.assertIn("待重新排队", updated.message)
        self.assertEqual(taskplan_router.list_requeue()["total"], 1)
        # 没有生效策略，重新排队被拒绝
        blocked = taskplan_router.run_action(plan.entry["id"], payload(action="重新排队"))
        self.assertFalse(blocked.ok)


if __name__ == "__main__":
    unittest.main()
