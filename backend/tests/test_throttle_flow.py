"""频率策略 → 采集计划排班 数据流的回归测试。

运行：backend/.venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services.taskplan import TaskplanService
from app.services.throttle import (
    STATUS_ACTIVE,
    STATUS_CANDIDATE,
    STATUS_DISABLED,
    STATUS_DRAFT,
    ThrottleService,
)
from app.store import store


def valid_values(**overrides):
    values = {
        "策略编号": "THRO-T1",
        "目标站点": "新闻资讯站A",
        "请求间隔": 500,
        "日请求上限": 2000,
        "并发数量": 2,
        "排队策略": "公平排队",
        "优先级权重": 10,
    }
    values.update(overrides)
    return values


class ThrottleValidationTest(unittest.TestCase):
    def setUp(self):
        # 每个用例从种子数据重新开始，避免用例间污染
        store.__init__()
        self.service = ThrottleService()

    # ---------- 保存前校验 ----------
    def test_required_fields_rejected_when_missing(self):
        entry, errors = self.service.create_entry({})
        self.assertIsNone(entry)
        for field in ("策略编号", "目标站点", "请求间隔", "日请求上限", "并发数量", "优先级权重"):
            self.assertTrue(any(field in msg for msg in errors), errors)

    def test_negative_interval_rejected(self):
        entry, errors = self.service.create_entry(valid_values(请求间隔=-1))
        self.assertIsNone(entry)
        self.assertTrue(any("请求间隔" in msg for msg in errors))

    def test_interval_below_site_minimum_rejected(self):
        # 新闻资讯站A 最小间隔 200ms
        entry, errors = self.service.create_entry(valid_values(请求间隔=150))
        self.assertIsNone(entry)
        self.assertTrue(any("最小间隔" in msg for msg in errors))

    def test_daily_limit_over_site_quota_rejected(self):
        # 新闻资讯站A 日硬配额 20000
        entry, errors = self.service.create_entry(valid_values(日请求上限=20001))
        self.assertIsNone(entry)
        self.assertTrue(any("日配额" in msg and "额度冲突" in msg for msg in errors))

    def test_concurrency_over_site_quota_rejected(self):
        # 新闻资讯站A 并发硬配额 8
        entry, errors = self.service.create_entry(valid_values(并发数量=9))
        self.assertIsNone(entry)
        self.assertTrue(any("并发配额" in msg for msg in errors))

    def test_weight_range_rejected(self):
        for bad in (0, 101, -5):
            entry, errors = self.service.create_entry(valid_values(策略编号=f"THRO-W{bad}", 优先级权重=bad))
            self.assertIsNone(entry, bad)
            self.assertTrue(any("优先级权重" in msg for msg in errors))

    def test_unknown_site_rejected(self):
        entry, errors = self.service.create_entry(valid_values(目标站点="不存在的站点"))
        self.assertIsNone(entry)
        self.assertTrue(any("站点目录" in msg for msg in errors))

    def test_non_numeric_values_rejected(self):
        entry, errors = self.service.create_entry(valid_values(请求间隔="abc"))
        self.assertIsNone(entry)
        self.assertTrue(any("请求间隔" in msg for msg in errors))

    def test_decimal_and_scientific_notation_rejected(self):
        for bad in ("1.5", "1e3", "+500", "0x10", True):
            entry, errors = self.service.create_entry(
                valid_values(策略编号=f"THRO-N{str(bad)}", 请求间隔=bad)
            )
            self.assertIsNone(entry, bad)
            self.assertTrue(any("请求间隔" in msg for msg in errors), bad)

    def test_string_integer_accepted(self):
        # 表单提交的数字字符串应被正常收敛为 int
        entry, errors = self.service.create_entry(valid_values(请求间隔="500"))
        self.assertEqual(errors, [])
        self.assertEqual(entry["请求间隔"], 500)

    def test_valid_policy_saved_as_draft(self):
        entry, errors = self.service.create_entry(valid_values())
        self.assertEqual(errors, [])
        self.assertEqual(entry["status"], STATUS_DRAFT)
        self.assertEqual(entry["请求间隔"], 500)

    def test_duplicate_code_create_rejected(self):
        self.service.create_entry(valid_values())
        entry, errors = self.service.create_entry(valid_values(日请求上限=1000))
        self.assertIsNone(entry)
        self.assertTrue(any("已存在" in msg for msg in errors))
        # 旧策略仍然存在，没有被覆盖丢失
        rows = store.rows("throttle")
        same_code = [r for r in rows if r["策略编号"] == "THRO-T1"]
        self.assertEqual(len(same_code), 1)
        self.assertEqual(same_code[0]["日请求上限"], 2000)


class ThrottleStateMachineTest(unittest.TestCase):
    def setUp(self):
        store.__init__()
        self.service = ThrottleService()
        entry, _ = self.service.create_entry(valid_values())
        self.entry_id = entry["id"]

    def test_full_flow_draft_candidate_active_disabled(self):
        entry, msg = self.service.run_action(self.entry_id, "提交校验")
        self.assertEqual(entry["status"], STATUS_CANDIDATE)
        entry, _ = self.service.run_action(self.entry_id, "启用策略")
        self.assertEqual(entry["status"], STATUS_ACTIVE)
        self.assertEqual(entry["是否启用"], "是")
        entry, _ = self.service.run_action(self.entry_id, "停用策略")
        self.assertEqual(entry["status"], STATUS_DISABLED)
        self.assertEqual(entry["是否启用"], "否")

    def test_cannot_skip_candidate_stage(self):
        # 待配置直接启用：拒绝
        entry, msg = self.service.run_action(self.entry_id, "启用策略")
        self.assertIsNone(entry)
        self.assertIn("不允许执行", msg)

    def test_duplicate_enable_advances_only_once(self):
        self.service.run_action(self.entry_id, "提交校验")
        first, _ = self.service.run_action(self.entry_id, "启用策略")
        self.assertEqual(first["status"], STATUS_ACTIVE)
        # 启用命令重复到达：幂等，不重复推进、不报错
        second, msg = self.service.run_action(self.entry_id, "启用策略")
        self.assertEqual(second["status"], STATUS_ACTIVE)
        self.assertIn("未重复推进", msg)

    def test_duplicate_submit_is_idempotent(self):
        self.service.run_action(self.entry_id, "提交校验")
        again, msg = self.service.run_action(self.entry_id, "启用策略")  # noqa: F841
        second, msg = self.service.run_action(self.entry_id, "提交校验")
        self.assertIsNone(second)
        self.assertIn("不允许执行", msg)

    def test_disabled_cannot_be_modified(self):
        self.service.run_action(self.entry_id, "提交校验")
        self.service.run_action(self.entry_id, "启用策略")
        self.service.run_action(self.entry_id, "停用策略")
        entry, errors = self.service.update_entry(self.entry_id, valid_values(请求间隔=800))
        self.assertIsNone(entry)
        self.assertTrue(any("已停用" in msg for msg in errors))

    def test_dirty_policy_fails_submit_check(self):
        # 手工构造一条绕过登记校验的脏数据，提交校验必须拦下
        rows = store.rows("throttle")
        rows.append({
            "id": 999, "status": STATUS_DRAFT, "pending": True, "abnormal": False,
            "策略编号": "THRO-DIRTY", "目标站点": "新闻资讯站A",
            "请求间隔": -10, "日请求上限": 999999, "并发数量": 0,
            "排队策略": "公平排队", "优先级权重": 0, "是否启用": "否",
        })
        entry, msg = self.service.run_action(999, "提交校验")
        self.assertIsNone(entry)
        self.assertIn("校验未通过", msg)


class SameSiteSingleActivePolicyTest(unittest.TestCase):
    def setUp(self):
        store.__init__()
        self.service = ThrottleService()
        # 站点 新闻资讯站A 的两条候选策略
        first, _ = self.service.create_entry(valid_values(策略编号="THRO-A1", 日请求上限=1000))
        self.first_id = first["id"]
        second, _ = self.service.create_entry(valid_values(策略编号="THRO-A2", 日请求上限=2000))
        self.second_id = second["id"]
        self.service.run_action(self.first_id, "提交校验")
        self.service.run_action(self.second_id, "提交校验")
        self.service.run_action(self.first_id, "启用策略")

    def test_second_enable_rejected_with_existing_code(self):
        entry, msg = self.service.run_action(self.second_id, "启用策略")
        self.assertIsNone(entry)
        # 必须指出现有生效策略编号
        self.assertIn("THRO-A1", msg)
        self.assertIn("只允许一条生效", msg)
        # 只有一条生效
        active = [r for r in store.rows("throttle") if r["status"] == STATUS_ACTIVE
                  and r["目标站点"] == "新闻资讯站A"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], self.first_id)

    def test_replace_policy_designates_new_candidate_as_active(self):
        # 显式指定以新候选为准
        entry, msg = self.service.run_action(self.second_id, "启用策略", replace_policy=True)
        self.assertEqual(entry["status"], STATUS_ACTIVE)
        old = self.service.get_entry(self.first_id)
        self.assertEqual(old["status"], STATUS_DISABLED)
        active = [r for r in store.rows("throttle") if r["status"] == STATUS_ACTIVE
                  and r["目标站点"] == "新闻资讯站A"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["id"], self.second_id)

    def test_active_policy_lookup_only_returns_active(self):
        policy = self.service.active_policy_for_site("新闻资讯站A")
        self.assertEqual(policy["id"], self.first_id)
        self.service.run_action(self.first_id, "停用策略")
        self.assertIsNone(self.service.active_policy_for_site("新闻资讯站A"))

    def test_different_sites_can_each_have_active_policy(self):
        # 另一个站点启用自己的策略不冲突（政务公开站D 在种子数据里没有策略）
        other, _ = self.service.create_entry(valid_values(
            策略编号="THRO-D1", 目标站点="政务公开站D",
            请求间隔=2000, 日请求上限=1000, 并发数量=1, 优先级权重=20,
        ))
        self.service.run_action(other["id"], "提交校验")
        entry, msg = self.service.run_action(other["id"], "启用策略")
        self.assertEqual(entry["status"], STATUS_ACTIVE)


class TaskplanSchedulingTest(unittest.TestCase):
    def setUp(self):
        store.__init__()
        self.throttle = ThrottleService()
        self.plans = TaskplanService()
        policy, _ = self.throttle.create_entry(valid_values(
            策略编号="THRO-S1", 目标站点="政务公开站D",
            请求间隔=2000, 日请求上限=1000, 并发数量=1, 优先级权重=5,
        ))
        self.policy_id = policy["id"]
        plan, _ = self.plans.create_entry({
            "计划编号": "TASK-S1", "目标站点": "政务公开站D", "采集频率": "每日一轮",
        })
        self.plan_id = plan["id"]

    def test_start_without_active_policy_rejected(self):
        # 策略还在待配置，排班找不到生效策略
        entry, msg = self.plans.run_action(self.plan_id, "启动计划")
        self.assertIsNone(entry)
        self.assertIn("没有生效中的频率策略", msg)

    def test_start_snapshots_active_policy_quota(self):
        self.throttle.run_action(self.policy_id, "提交校验")
        self.throttle.run_action(self.policy_id, "启用策略")
        entry, msg = self.plans.run_action(self.plan_id, "启动计划")
        self.assertEqual(entry["status"], "运行中")
        self.assertEqual(entry["throttle_id"], self.policy_id)
        self.assertIn("THRO-S1", entry["资源配额"])
        self.assertIn("日上限1000", entry["资源配额"])

    def test_disabled_policy_requeues_plans_and_invalidates_quota(self):
        self.throttle.run_action(self.policy_id, "提交校验")
        self.throttle.run_action(self.policy_id, "启用策略")
        self.plans.run_action(self.plan_id, "启动计划")
        # 真实链路：先停用策略（状态转停用，active_policy_for_site 立即查不到），
        # 路由层再联动驱逐引用该额度的计划
        disabled, msg = self.throttle.run_action(self.policy_id, "停用策略")
        self.assertEqual(disabled["status"], STATUS_DISABLED)
        affected = self.plans.evict_policy(self.policy_id)
        self.assertEqual(len(affected), 1)
        plan = self.plans.get_entry(self.plan_id)
        self.assertEqual(plan["status"], "待重新排队")
        self.assertIn("待重新排队", plan["资源配额"])
        # 另一侧不能再按已停用规则排队
        entry, msg = self.plans.run_action(self.plan_id, "重新排队")
        self.assertIsNone(entry)
        self.assertIn("没有生效中的频率策略", msg)
        # 排队待办清单跟着刷新
        queue = self.plans.requeue_entries()
        self.assertEqual([item["id"] for item in queue], [self.plan_id])

    def test_requeue_picks_up_new_active_policy(self):
        self.throttle.run_action(self.policy_id, "提交校验")
        self.throttle.run_action(self.policy_id, "启用策略")
        self.plans.run_action(self.plan_id, "启动计划")
        self.throttle.run_action(self.policy_id, "停用策略")
        self.plans.evict_policy(self.policy_id)
        # 登记并启用新策略
        new_policy, _ = self.throttle.create_entry(valid_values(
            策略编号="THRO-S2", 目标站点="政务公开站D",
            请求间隔=3000, 日请求上限=500, 并发数量=1, 优先级权重=8,
        ))
        self.throttle.run_action(new_policy["id"], "提交校验")
        self.throttle.run_action(new_policy["id"], "启用策略", replace_policy=True)
        entry, msg = self.plans.run_action(self.plan_id, "重新排队")
        self.assertEqual(entry["status"], "运行中")
        self.assertEqual(entry["throttle_id"], new_policy["id"])
        self.assertIn("THRO-S2", entry["资源配额"])
        # 排队待办清空
        self.assertEqual(self.plans.requeue_entries(), [])

    def test_plan_unknown_site_rejected(self):
        entry, errors = self.plans.create_entry({
            "计划编号": "TASK-X", "目标站点": "不存在的站点", "采集频率": "每日一轮",
        })
        self.assertIsNone(entry)
        self.assertTrue(any("站点目录" in msg for msg in errors))

    def test_duplicate_plan_code_rejected(self):
        entry, errors = self.plans.create_entry({
            "计划编号": "TASK-S1", "目标站点": "政务公开站D", "采集频率": "每日一轮",
        })
        self.assertIsNone(entry)
        self.assertTrue(any("已存在" in msg for msg in errors))


class ListDetailConsistencyTest(unittest.TestCase):
    def setUp(self):
        store.__init__()
        self.service = ThrottleService()

    def test_list_total_matches_detail_count(self):
        for idx in range(3):
            self.service.create_entry(valid_values(
                策略编号=f"THRO-C{idx}", 目标站点=("新闻资讯站A", "电商商品站B", "论坛社区站C")[idx],
                请求间隔=(300, 600, 1200)[idx], 日请求上限=100, 并发数量=1, 优先级权重=10,
            ))
        page_one, total = self.service.list_entries(page=1, size=2)
        self.assertEqual(len(page_one), 2)
        page_two, _ = self.service.list_entries(page=2, size=2)
        self.assertEqual(len(page_two), 2)  # 种子 3 条 + 新增 3 条 = 6
        self.assertEqual(total, 6)
        all_rows, _ = self.service.list_entries(page=1, size=100)
        # 每条列表记录都能按 id 取到详情，数量一致
        for row in all_rows:
            self.assertIsNotNone(self.service.get_entry(int(row["id"])))
        self.assertEqual(len(all_rows), total)


if __name__ == "__main__":
    unittest.main()
