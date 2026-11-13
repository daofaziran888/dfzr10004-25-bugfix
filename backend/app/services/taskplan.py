"""采集任务编排业务规则。

排班与频率策略的数据传递口径：
- 启动计划时必须能按目标站点找到「生效」频率策略，并把当时的请求间隔、日请求上限、
  并发数量、优先级权重快照进计划（资源配额）；找不到生效策略一律拒绝启动。
- 频率策略被停用后，引用该策略的计划进入「待重新排队」，旧额度快照作废，
  重新排队时重新解析当前唯一生效策略；另一侧不能再按已停用规则排队。
"""
from __future__ import annotations

from typing import Any

from app import catalog
from app.services.throttle import ThrottleService
from app.store import store

MODULE = "taskplan"
REQUIRED_FIELDS = ["计划编号", "目标站点", "采集频率"]

STATUS_DRAFT = "草稿"
STATUS_RUNNING = "运行中"
STATUS_PAUSED = "已暂停"
STATUS_DONE = "已完成"
STATUS_REQUEUE = "待重新排队"
# 状态序列（新增待重新排队：频率策略停用后的排队待办）
STATUS_ORDER = [STATUS_DRAFT, STATUS_RUNNING, STATUS_PAUSED, STATUS_DONE, STATUS_REQUEUE]

ACTION_START = "启动计划"
ACTION_PAUSE = "暂停计划"
ACTION_ARCHIVE = "归档计划"
ACTION_REQUEUE = "重新排队"
ACTION_RULES = {
    ACTION_START: STATUS_RUNNING,
    ACTION_PAUSE: STATUS_PAUSED,
    ACTION_ARCHIVE: STATUS_DONE,
    ACTION_REQUEUE: STATUS_RUNNING,
}
ACTION_SOURCE = {
    ACTION_START: {STATUS_DRAFT},
    ACTION_PAUSE: {STATUS_RUNNING},
    ACTION_ARCHIVE: {STATUS_PAUSED},
    ACTION_REQUEUE: {STATUS_REQUEUE},
}
NEGATIVE_ACTIONS: list[str] = []

throttle_service = ThrottleService()


class TaskplanService:
    # ---------- 查询 ----------
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        site: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = list(store.rows(MODULE))
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if site:
            site_name = catalog.normalize_site(site)
            rows = [row for row in rows if catalog.normalize_site(row.get("目标站点")) == site_name]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def requeue_entries(self) -> list[dict[str, Any]]:
        """排队待办：所有待重新排队的计划，频率策略侧动作后这里跟着刷新。"""
        return [row for row in store.rows(MODULE) if row.get("status") == STATUS_REQUEUE]

    # ---------- 登记 ----------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        site_name = catalog.normalize_site(values.get("目标站点"))
        if catalog.get_site(site_name) is None:
            allowed = "、".join(item.name for item in catalog.SITE_CATALOG)
            return None, [f"目标站点「{site_name}」不在站点目录内，允许的站点：{allowed}"]
        rows = store.rows(MODULE)
        for row in rows:
            if str(row.get("计划编号", "")).strip() == str(values.get("计划编号") or "").strip():
                return None, [f"计划编号「{values.get('计划编号')}」已存在，不能重复登记"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["目标站点"] = site_name
        entry["数据范围"] = str(values.get("数据范围") or "").strip() or "全量"
        entry["优先级"] = str(values.get("优先级") or "").strip() or "普通"
        entry["资源配额"] = "待排班"
        entry["预计用时"] = str(values.get("预计用时") or "").strip() or "—"
        entry["计划状态"] = STATUS_DRAFT
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["throttle_id"] = None
        entry["throttle_code"] = None
        rows.append(entry)
        return entry, []

    # ---------- 频率策略联动 ----------
    def evict_policy(self, throttle_id: int) -> list[dict[str, Any]]:
        """频率策略停用后调用：引用该策略额度的计划全部转入待重新排队，旧额度作废。"""
        affected: list[dict[str, Any]] = []
        for row in store.rows(MODULE):
            if row.get("throttle_id") != throttle_id:
                continue
            if row.get("status") in (STATUS_RUNNING, STATUS_PAUSED):
                row["status"] = STATUS_REQUEUE
                row["计划状态"] = STATUS_REQUEUE
                row["pending"] = True
                row["abnormal"] = True
                row["资源配额"] = "原额度已停用，待重新排队"
                affected.append(row)
        return affected

    def _snapshot_quota(self, policy: dict[str, Any]) -> str:
        return (
            f"策略{policy.get('策略编号')}｜间隔{policy.get('请求间隔')}ms｜"
            f"日上限{policy.get('日请求上限')}｜并发{policy.get('并发数量')}｜"
            f"权重{policy.get('优先级权重')}"
        )

    # ---------- 状态流转 ----------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find_live(MODULE, entry_id)
        if entry is None:
            return None, f"采集计划 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集任务编排可执行范围"

        current = entry.get("status")
        target = ACTION_RULES[action]
        if current == target:
            return entry, f"采集计划已是「{target}」状态，命令未重复推进"
        if current not in ACTION_SOURCE[action]:
            allowed = "、".join(sorted(ACTION_SOURCE[action]))
            return None, f"计划当前为「{current}」，不允许执行「{action}」；该动作仅允许来源状态：{allowed}"

        if action in (ACTION_START, ACTION_REQUEUE):
            # 排班唯一取数口径：按站点解析当前唯一生效策略，停用规则查不到
            policy = throttle_service.active_policy_for_site(entry.get("目标站点", ""))
            if policy is None:
                return None, (
                    f"站点「{entry.get('目标站点')}」当前没有生效中的频率策略，"
                    "无法确定限流额度，请先在采集频率控制启用策略后再排队"
                )
            entry["throttle_id"] = policy.get("id")
            entry["throttle_code"] = policy.get("策略编号")
            entry["资源配额"] = self._snapshot_quota(policy)
            entry["status"] = STATUS_RUNNING
            entry["计划状态"] = STATUS_RUNNING
            entry["pending"] = True
            entry["abnormal"] = False
            verb = "重新排队完成" if action == ACTION_REQUEUE else "已启动并完成排班"
            return entry, f"采集计划{verb}，按生效策略「{policy.get('策略编号')}」锁定限流额度"

        if action == ACTION_PAUSE:
            entry["status"] = STATUS_PAUSED
            entry["计划状态"] = STATUS_PAUSED
            entry["pending"] = True
            return entry, "采集计划已暂停"

        # ACTION_ARCHIVE
        entry["status"] = STATUS_DONE
        entry["计划状态"] = STATUS_DONE
        entry["pending"] = False
        return entry, "采集计划已归档"
