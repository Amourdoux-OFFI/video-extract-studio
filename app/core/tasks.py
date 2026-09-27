# -*- coding: utf-8 -*-
"""任务队列 + 进度事件总线（供 SSE 推送）。

所有耗时操作（解析/下载/合流/裁剪/压缩）都是一个 Task；
进度可以从任意线程上报，内部用 call_soon_threadsafe 转回事件循环。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

TERMINAL = ("done", "error", "canceled")


@dataclass
class Task:
    id: str
    kind: str  # resolve|download|mux|trim|compress|import
    title: str = ""
    status: str = "pending"  # pending|running|done|error|canceled
    phase: str = ""
    percent: float = 0.0
    speed: float = 0.0
    eta: float = 0.0
    message: str = ""
    error: str = ""
    result: dict = field(default_factory=dict)
    created: float = 0.0
    updated: float = 0.0

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._queues: set[asyncio.Queue] = set()
        self._cancel: dict[str, threading.Event] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.RLock()

    # ---------- 生命周期 ----------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def new(self, kind: str, title: str = "", **extra) -> Task:
        now = time.time()
        task = Task(
            id=f"t-{uuid.uuid4().hex[:10]}",
            kind=kind,
            title=title,
            created=now,
            updated=now,
            **extra,
        )
        with self._lock:
            self._tasks[task.id] = task
            self._cancel[task.id] = threading.Event()
        self.publish(task)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def all(self) -> list[dict]:
        with self._lock:
            items = sorted(self._tasks.values(), key=lambda t: t.created)
        return [asdict(t) for t in items]

    def cancel_event(self, task_id: str) -> threading.Event:
        with self._lock:
            ev = self._cancel.get(task_id)
            if ev is None:
                ev = threading.Event()
                self._cancel[task_id] = ev
            return ev

    def is_canceled(self, task_id: str) -> bool:
        return self.cancel_event(task_id).is_set()

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
        if not task or task.finished:
            return False
        self.cancel_event(task_id).set()
        self.update(task_id, status="canceled", phase="已取消", message="用户取消")
        return True

    def cancel_all(self) -> int:
        with self._lock:
            ids = [t.id for t in self._tasks.values() if not t.finished]
        return sum(1 for i in ids if self.cancel(i))

    def clear(self, task_id: str | None = None, finished_only: bool = False) -> int:
        with self._lock:
            if task_id:
                victims = [task_id] if self._tasks.get(task_id) else []
            else:
                victims = [
                    t.id
                    for t in self._tasks.values()
                    if (t.finished if finished_only else True)
                ]
            for vid in victims:
                self._tasks.pop(vid, None)
                self._cancel.pop(vid, None)
        if victims:
            self._broadcast({"type": "cleared", "ids": victims})
        return len(victims)

    # ---------- 状态更新 ----------

    def _apply(self, task: Task, fields: dict) -> None:
        for k, v in fields.items():
            if v is not None and hasattr(task, k):
                setattr(task, k, v)

    def update(self, task_id: str, **fields) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            self._apply(task, fields)
            task.updated = time.time()
        self.publish(task)

    def progress(
        self,
        task_id: str,
        percent: float | None = None,
        *,
        phase: str | None = None,
        speed: float | None = None,
        eta: float | None = None,
        message: str | None = None,
    ) -> None:
        fields: dict = {}
        if percent is not None:
            fields["percent"] = max(0.0, min(100.0, float(percent)))
        if phase is not None:
            fields["phase"] = phase
        if speed is not None:
            fields["speed"] = float(speed)
        if eta is not None:
            fields["eta"] = float(eta)
        if message is not None:
            fields["message"] = message
        if fields:
            self.update(task_id, **fields)

    def start(self, task_id: str, phase: str = "") -> None:
        self.update(task_id, status="running", phase=phase or "进行中", error="")

    def done(self, task_id: str, **result) -> None:
        self.update(
            task_id,
            status="done",
            percent=100.0,
            phase="完成",
            speed=0.0,
            eta=0.0,
            result=result,
        )

    def fail(self, task_id: str, error: str, phase: str = "失败") -> None:
        self.update(task_id, status="error", phase=phase, error=str(error), speed=0.0, eta=0.0)

    # ---------- 事件分发 ----------

    def subscribe(self, maxsize: int = 512) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._queues.discard(q)

    @staticmethod
    def _safe_put(q: asyncio.Queue, payload: dict) -> None:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(payload)
            except Exception:  # noqa: BLE001
                pass

    def _broadcast(self, payload: dict) -> None:
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                loop = self._loop
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(self._safe_put, q, payload)
                else:
                    self._safe_put(q, payload)
            except RuntimeError:
                pass

    def publish(self, task: Task) -> None:
        self._broadcast({"type": "task", "task": asdict(task)})


manager = TaskManager()
