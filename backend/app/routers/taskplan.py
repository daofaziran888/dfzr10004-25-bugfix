"""采集任务编排接口：维护采集计划，覆盖启动计划、暂停计划、归档计划、重新排队等动作。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.taskplan import TaskplanService

router = APIRouter(prefix="/api/taskplan", tags=["采集任务编排"])

service = TaskplanService()

LIST_FIELDS = ["计划编号", "目标站点", "采集频率", "数据范围", "优先级", "资源配额", "预计用时", "计划状态"]
STATUSES = ["草稿", "运行中", "已暂停", "已完成", "待重新排队"]


@router.get("/requeue-queue")
def list_requeue() -> dict:
    """排队待办清单：频率策略停用后这里会跟着刷新，另一侧不能再按已停用规则排队。"""
    items = service.requeue_entries()
    return {"total": len(items), "items": items}


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按计划编号检索"),
    status: str | None = Query(default=None, description="草稿、运行中、已暂停、已完成、待重新排队"),
    site: str | None = Query(default=None, description="按目标站点精确过滤"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按计划编号、状态与站点过滤采集任务编排列表；没有数据时返回空页，不报错。"""
    if page < 1:
        raise HTTPException(status_code=400, detail="页码必须从 1 开始")
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, site=site, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict:
    """导出采集任务编排清单：返回全量数据，口径与分页列表完全一致。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "taskplan", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条采集计划明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"采集计划 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条采集计划，缺字段或站点不在目录时说明原因而不是静默丢弃。"""
    entry, errors = service.create_entry(payload.values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="采集计划已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """启动/暂停/归档/重新排队。

    启动与重新排队都会按目标站点重新解析当前唯一生效的频率策略并快照额度；
    站点没有生效策略（例如规则已停用）时拒绝排队。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
