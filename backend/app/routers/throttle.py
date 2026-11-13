"""采集频率控制接口：维护频率策略，覆盖提交校验、启用策略、停用策略等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.catalog import site_dicts
from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.taskplan import TaskplanService
from app.services.throttle import ThrottleService

router = APIRouter(prefix="/api/throttle", tags=["采集频率控制"])

service = ThrottleService()
taskplan_service = TaskplanService()

LIST_FIELDS = ["策略编号", "目标站点", "请求间隔", "日请求上限", "并发数量", "排队策略", "优先级权重", "是否启用"]
STATUSES = ["待配置", "候选", "生效", "停用"]


@router.get("/sites")
def list_sites() -> dict[str, Any]:
    """站点配额目录：登记策略前先查允许的站点及其日配额、并发配额、最小间隔。"""
    return {"items": site_dicts()}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="待配置、候选、生效、停用"),
    site: str | None = Query(default=None, description="按目标站点精确过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号、状态与站点过滤采集频率控制列表；没有数据时返回空页，不报错。"""
    if page < 1:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, site=site, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出采集频率控制清单：返回全量数据，口径与分页列表完全一致。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "throttle", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条频率策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"频率策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条频率策略。

    保存前必须校验目标站点、请求间隔、日请求上限、并发数量、优先级权重；
    不符合条件（含负数间隔、超过站点配额的日上限）一律拒绝，不产生脏数据。
    """
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="频率策略已登记，当前为待配置状态", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改既有频率策略；重复保存走该接口，旧策略不会丢失。

    生效策略被修改后退回待配置，其额度同步失效，引用该额度的计划转待重新排队。
    """
    before = service.get_entry(entry_id)
    entry, errors = service.update_entry(entry_id, payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    message = "频率策略已修改，字段变更后退回待配置待重新校验"
    if before is not None and before.get("status") == "生效":
        affected = len(taskplan_service.evict_policy(entry_id))
        if affected:
            message = f"{message}；{affected} 条引用原额度的采集计划已转入待重新排队"
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条频率策略执行提交校验、启用策略、停用策略。

    - 状态只能按 待配置→候选→生效→停用 单向流转，重复命令幂等只推进一次；
    - 同站点同时只允许一条生效策略，冲突时拒绝并指出现有策略编号，
      values.replace_policy=true 时以本次显式指定的候选为准；
    - 停用（含被新策略替换而隐式停用）后旧额度立即失效，
      引用该额度的采集计划转入待重新排队。
    """
    action = str(payload.values.get("action") or "").strip()
    replace_policy = bool(payload.values.get("replace_policy"))
    # 启用前先记下同站点现有生效策略，替换成功后要联动驱逐引用旧额度的计划
    existing = None
    if action == "启用策略":
        target = service.get_entry(entry_id)
        if target is not None:
            existing = service.active_policy_for_site(target.get("目标站点", ""))
    entry, message = service.run_action(entry_id, action, replace_policy=replace_policy)
    if entry is None:
        return ActionResult(ok=False, message=message)
    affected = 0
    if action == "停用策略" and entry.get("status") == "停用":
        affected = len(taskplan_service.evict_policy(entry_id))
    elif (
        action == "启用策略"
        and entry.get("status") == "生效"
        and existing is not None
        and int(existing.get("id", 0)) != entry_id
    ):
        affected = len(taskplan_service.evict_policy(int(existing["id"])))
    if affected:
        message = f"{message}；{affected} 条采集计划已转入待重新排队"
    return ActionResult(ok=True, message=message, entry=entry)
