import time
import uuid
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FlowItem:
    index: int
    status: str = "pending"  # pending|processing|completed|failed
    error: Optional[str] = None
    artifacts_dir: Optional[str] = None
    analysis: Optional[Dict[str, Any]] = None


@dataclass
class Flow:
    flow_id: str
    status: str = "processing"  # processing|completed|partial_failed|failed|cancelled
    created_at: float = field(default_factory=lambda: time.time())
    updated_at: float = field(default_factory=lambda: time.time())
    completed_at: Optional[float] = None

    exaggeration_level: str = "明显"
    align_reference: bool = True
    shadow: bool = True

    total: int = 0
    completed: int = 0
    failed: int = 0
    progress: float = 0.0

    items: List[FlowItem] = field(default_factory=list)


_flow_store: Dict[str, Flow] = {}
_flow_lock = threading.Lock()


def create_flow(total: int, exaggeration_level: str, align_reference: bool, shadow: bool) -> Flow:
    flow_id = uuid.uuid4().hex
    flow = Flow(
        flow_id=flow_id,
        total=total,
        exaggeration_level=exaggeration_level,
        align_reference=align_reference,
        shadow=shadow,
        items=[FlowItem(index=i) for i in range(total)],
    )
    with _flow_lock:
        _flow_store[flow_id] = flow
    return flow


def get_flow(flow_id: str) -> Optional[Flow]:
    with _flow_lock:
        return _flow_store.get(flow_id)


def update_item(flow_id: str, index: int, **patch: Any) -> None:
    with _flow_lock:
        flow = _flow_store.get(flow_id)
        if not flow:
            return
        if index < 0 or index >= len(flow.items):
            return
        item = flow.items[index]
        for k, v in patch.items():
            setattr(item, k, v)
        flow.updated_at = time.time()


def recompute(flow_id: str) -> None:
    with _flow_lock:
        flow = _flow_store.get(flow_id)
        if not flow:
            return
        total = len(flow.items)
        completed = sum(1 for it in flow.items if it.status == "completed")
        failed = sum(1 for it in flow.items if it.status == "failed")
        processing = sum(1 for it in flow.items if it.status in {"processing", "pending"})
        flow.completed = completed
        flow.failed = failed
        flow.total = total
        flow.progress = (completed + failed) / total if total else 0.0

        if flow.status == "cancelled":
            return
        if processing > 0:
            flow.status = "processing"
            flow.completed_at = None
            return
        # terminal
        if completed == total:
            flow.status = "completed"
        elif completed > 0:
            flow.status = "partial_failed"
        else:
            flow.status = "failed"
        flow.completed_at = time.time()


def cancel_flow(flow_id: str) -> bool:
    with _flow_lock:
        flow = _flow_store.get(flow_id)
        if not flow:
            return False
        flow.status = "cancelled"
        flow.completed_at = time.time()
        flow.updated_at = time.time()
        return True
