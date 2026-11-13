"""采集频率控制业务规则。

状态机（单向流转，重复命令幂等）：
    待配置 --提交校验--> 候选 --启用策略--> 生效 --停用策略--> 停用

约束口径：
- 同站点同时只允许一条「生效」策略；启用冲突时拒绝并指出现有生效策略编号，
  显式携带 replace_policy=true 才以本次指定的候选为准（旧生效策略转停用）。
- 保存（登记/修改）前必须校验目标站点、请求间隔、日请求上限、并发数量、优先级权重，
  不满足条件的策略不允许保存，更不允许启用。
- 停用后的额度立即失效，采集计划侧不能再按旧额度排队（见 services/taskplan.py）。
"""
from __future__ import annotations

import re
from typing import Any

from app import catalog
from app.store import store

MODULE = "throttle"

# 业务字段（与前端列表、路由 LIST_FIELDS 对齐）
ALL_FIELDS = ["策略编号", "目标站点", "请求间隔", "日请求上限", "并发数量", "排队策略", "优先级权重"]
REQUIRED_FIELDS = ["策略编号", "目标站点", "请求间隔", "日请求上限", "并发数量", "优先级权重"]

# 状态序列：待配置 → 候选 → 生效 → 停用
STATUS_DRAFT = "待配置"
STATUS_CANDIDATE = "候选"
STATUS_ACTIVE = "生效"
STATUS_DISABLED = "停用"
STATUS_ORDER = [STATUS_DRAFT, STATUS_CANDIDATE, STATUS_ACTIVE, STATUS_DISABLED]

# 动作 → 目标状态
ACTION_SUBMIT = "提交校验"
ACTION_ENABLE = "启用策略"
ACTION_DISABLE = "停用策略"
ACTION_RULES = {
    ACTION_SUBMIT: STATUS_CANDIDATE,
    ACTION_ENABLE: STATUS_ACTIVE,
    ACTION_DISABLE: STATUS_DISABLED,
}
# 每个动作允许的来源状态；不在来源状态里的重复命令直接幂等返回
ACTION_SOURCE = {
    ACTION_SUBMIT: {STATUS_DRAFT},
    ACTION_ENABLE: {STATUS_CANDIDATE},
    ACTION_DISABLE: {STATUS_ACTIVE},
}

# 数值字段允许范围（站点相关的间隔/上限/并发还要再与站点硬配额比对）
INTERVAL_MIN_MS = 100          # 请求间隔阈值：100ms~60000ms
INTERVAL_MAX_MS = 60_000
DAILY_LIMIT_MIN = 1            # 日请求上限允许范围：1~100000，且不得超过站点硬配额
DAILY_LIMIT_MAX = 100_000
CONCURRENCY_MIN = 1            # 并发数量允许范围：1~50，且不得超过站点硬配额
CONCURRENCY_MAX = 50
WEIGHT_MIN = 1                 # 优先级权重允许范围：1~100 的正整数
WEIGHT_MAX = 100


def _to_int(value: Any, field: str) -> tuple[int | None, str | None]:
    """把前端提交的数值字段收敛成 int；布尔、空串、小数、科学计数法一律拒绝。"""
    if isinstance(value, bool):
        return None, f"{field}必须是整数"
    if isinstance(value, int):
        return value, None
    text = str(value if value is not None else "").strip()
    if not text:
        return None, f"{field}不能为空"
    # 只接受「可选负号 + 纯数字」的十进制整数写法，拒绝 1.5、1e3、+5、0x10 等
    if re.fullmatch(r"-?\d+", text) is None:
        return None, f"{field}必须是整数，收到「{text}」"
    return int(text), None


def validate_payload(values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """保存前的统一校验。返回 (清洗后的字段, 错误说明列表)。"""
    errors: list[str] = []
    cleaned: dict[str, Any] = {}

    # 1. 文本必填：策略编号、目标站点
    code = str(values.get("策略编号") or "").strip()
    if not code:
        errors.append("策略编号不能为空")
    else:
        cleaned["策略编号"] = code

    raw_site = values.get("目标站点")
    site_name = catalog.normalize_site(raw_site)
    if not site_name:
        errors.append("目标站点不能为空")
        site = None
    else:
        site = catalog.get_site(site_name)
        if site is None:
            allowed = "、".join(item.name for item in catalog.SITE_CATALOG)
            errors.append(f"目标站点「{site_name}」不在站点目录内，允许的站点：{allowed}")
        else:
            cleaned["目标站点"] = site.name

    # 2. 请求间隔：100~60000ms 整数，且不低于站点最小间隔
    interval, err = _to_int(values.get("请求间隔"), "请求间隔")
    if err:
        errors.append(err)
    elif not (INTERVAL_MIN_MS <= interval <= INTERVAL_MAX_MS):
        errors.append(f"请求间隔必须在 {INTERVAL_MIN_MS}~{INTERVAL_MAX_MS} 毫秒之间，收到 {interval}")
    elif site is not None and interval < site.min_interval:
        errors.append(
            f"请求间隔 {interval}ms 低于站点「{site.name}」要求的最小间隔 {site.min_interval}ms"
        )
    else:
        cleaned["请求间隔"] = interval

    # 3. 日请求上限：1~100000 正整数，且不得超过站点硬配额
    daily_limit, err = _to_int(values.get("日请求上限"), "日请求上限")
    if err:
        errors.append(err)
    elif not (DAILY_LIMIT_MIN <= daily_limit <= DAILY_LIMIT_MAX):
        errors.append(f"日请求上限必须在 {DAILY_LIMIT_MIN}~{DAILY_LIMIT_MAX} 之间，收到 {daily_limit}")
    elif site is not None and daily_limit > site.daily_cap:
        errors.append(
            f"日请求上限 {daily_limit} 超过站点「{site.name}」日配额 {site.daily_cap}，额度冲突"
        )
    else:
        cleaned["日请求上限"] = daily_limit

    # 4. 并发数量：1~50 正整数，且不得超过站点并发硬配额
    concurrency, err = _to_int(values.get("并发数量"), "并发数量")
    if err:
        errors.append(err)
    elif not (CONCURRENCY_MIN <= concurrency <= CONCURRENCY_MAX):
        errors.append(f"并发数量必须在 {CONCURRENCY_MIN}~{CONCURRENCY_MAX} 之间，收到 {concurrency}")
    elif site is not None and concurrency > site.max_concurrency:
        errors.append(
            f"并发数量 {concurrency} 超过站点「{site.name}」并发配额 {site.max_concurrency}，额度冲突"
        )
    else:
        cleaned["并发数量"] = concurrency

    # 5. 优先级权重：1~100 正整数
    weight, err = _to_int(values.get("优先级权重"), "优先级权重")
    if err:
        errors.append(err)
    elif not (WEIGHT_MIN <= weight <= WEIGHT_MAX):
        errors.append(f"优先级权重必须在 {WEIGHT_MIN}~{WEIGHT_MAX} 之间，收到 {weight}")
    else:
        cleaned["优先级权重"] = weight

    # 排队策略为可选文本，给默认值
    queue_policy = str(values.get("排队策略") or "").strip() or "公平排队"
    cleaned["排队策略"] = queue_policy

    if errors:
        return None, errors
    return cleaned, []


class ThrottleService:
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
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
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

    def active_policy_for_site(self, site: str) -> dict[str, Any] | None:
        """计划排班取额度的唯一入口：只认「生效」状态的策略，停用后的旧额度查不到。"""
        site_name = catalog.normalize_site(site)
        for row in store.rows(MODULE):
            if row.get("status") == STATUS_ACTIVE and catalog.normalize_site(row.get("目标站点")) == site_name:
                return row
        return None

    def _find_other_by_code(self, code: str, exclude_id: int | None) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if exclude_id is not None and int(row.get("id", 0)) == exclude_id:
                continue
            if str(row.get("策略编号", "")).strip() == code:
                return row
        return None

    # ---------- 登记 / 修改 ----------
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        cleaned, errors = validate_payload(values)
        if errors:
            return None, errors
        rows = store.rows(MODULE)
        if self._find_other_by_code(cleaned["策略编号"], None) is not None:
            return None, [f"策略编号「{cleaned['策略编号']}」已存在，重复登记会覆盖旧策略，请改用修改接口"]
        entry: dict[str, Any] = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update(cleaned)
        # 新登记的频率策略先处于待配置
        entry["status"] = STATUS_DRAFT
        entry["pending"] = True
        entry["abnormal"] = False
        entry["是否启用"] = "否"
        rows.append(entry)
        return entry, []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """修改既有策略。已停用策略不可改；生效/候选策略修改后退回待配置重新走校验。"""
        entry = store.find_live(MODULE, entry_id)
        if entry is None:
            return None, [f"频率策略 {entry_id} 不存在或已归档"]
        if entry.get("status") == STATUS_DISABLED:
            return None, [f"策略「{entry.get('策略编号')}」已停用，不能修改；如需调整请登记新策略"]
        merged = {field: entry.get(field) for field in ALL_FIELDS}
        merged.update({k: v for k, v in values.items() if k in ALL_FIELDS})
        cleaned, errors = validate_payload(merged)
        if errors:
            return None, errors
        duplicate = self._find_other_by_code(cleaned["策略编号"], entry_id)
        if duplicate is not None:
            return None, [f"策略编号「{cleaned['策略编号']}」已被策略 {duplicate.get('id')} 占用"]
        entry.update(cleaned)
        # 字段变更后原校验/生效结论失效，回到待配置；生效策略被改后额度同步失效
        if entry.get("status") != STATUS_DRAFT:
            entry["status"] = STATUS_DRAFT
            entry["pending"] = True
            entry["是否启用"] = "否"
        return entry, []

    # ---------- 状态流转 ----------
    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        replace_policy: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find_live(MODULE, entry_id)
        if entry is None:
            return None, f"频率策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集频率控制可执行范围"

        current = entry.get("status")
        target = ACTION_RULES[action]

        # 幂等：重复到达的命令只推进一次，不报错、不重复生效
        if current == target:
            return entry, f"频率策略已是「{target}」状态，命令未重复推进"
        if current not in ACTION_SOURCE[action]:
            allowed = "、".join(sorted(ACTION_SOURCE[action]))
            return None, f"策略当前为「{current}」，不允许执行「{action}」；该动作仅允许来源状态：{allowed}"

        if action == ACTION_SUBMIT:
            # 提交校验前再跑一遍完整字段校验，防止历史脏数据直接进候选
            cleaned, errors = validate_payload({field: entry.get(field) for field in ALL_FIELDS})
            if errors:
                return None, "校验未通过：" + "；".join(errors)
            entry.update(cleaned)
            entry["status"] = STATUS_CANDIDATE
            entry["pending"] = True
            return entry, "频率策略校验通过，已进入候选"

        if action == ACTION_ENABLE:
            site_name = catalog.normalize_site(entry.get("目标站点"))
            existing = self.active_policy_for_site(site_name)
            if existing is not None and int(existing.get("id", 0)) != entry_id:
                if not replace_policy:
                    return None, (
                        f"站点「{site_name}」已存在生效策略「{existing.get('策略编号')}」"
                        f"（编号 {existing.get('id')}），同站点同时只允许一条生效策略；"
                        "如确认以本次候选策略为准，请显式携带 replace_policy=true 重新启用"
                    )
                # 显式指定以新候选为准：旧生效策略转停用，额度立即失效
                existing["status"] = STATUS_DISABLED
                existing["pending"] = False
                existing["abnormal"] = True
                existing["是否启用"] = "否"
            entry["status"] = STATUS_ACTIVE
            entry["pending"] = False
            entry["abnormal"] = False
            entry["是否启用"] = "是"
            return entry, f"频率策略「{entry.get('策略编号')}」已生效，成为站点「{site_name}」的唯一生效策略"

        # ACTION_DISABLE：停用后旧额度立即失效，计划侧通过 active_policy_for_site 查不到它
        entry["status"] = STATUS_DISABLED
        entry["pending"] = False
        entry["abnormal"] = True
        entry["是否启用"] = "否"
        return entry, f"频率策略「{entry.get('策略编号')}」已停用，其额度不再参与计划排队"
