# -*- coding: utf-8 -*-
"""
P8 委托任务

源实现的机制（已在源码中核对）：
  * 任务字段：id / name / commands / interval / triggerTiming / floorType / disabled / scope
    - interval=0 表示手动触发；
    - triggerTiming ∈ {after_ai, after_user}（消息后触发）；
    - floorType ∈ {all, user, llm}（按楼层类型取当前楼层）；
    - scope ∈ {global, character, preset}。
  * 触发链：onMessageReceived → shouldSkipByContext → matchInterval → __runTaskSingleInstance
    → executeCommands（内含 <<taskjs>>...</taskjs>> 内联脚本块）。
  * 执行保护：按任务名单实例锁（同一任务不并发）、冷却（TASK_COOLDOWN）、
    已处理消息去重（chatId_messageId_sendDate 作 key）、可 resetTaskRun 复位。
  * 楼层计数：getFloorCounts（user/llm/total）→ pickFloorByType 取当前楼层。

QQX_AIBot 没有浏览器 JS 沙箱，因此把「执行」抽象成两种可插拔方式：
  1) 文本指令 —— commands 直接作为任务提示注入 Prompt（最常见用法：委托角色去做某事）；
  2) Python 处理器 —— 注册 callable，由业务侧决定真正动作。
调度、锁、冷却、去重、楼层语义全部保持与源实现一致。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

VALID_FLOOR = ("all", "user", "llm")
VALID_TIMING = ("after_ai", "after_user", "before_ai")
VALID_SCOPE = ("global", "character", "preset")
TASK_COOLDOWN = 3.0          # 秒；同一任务两次执行的冷却
TASKS_FILE = Path("tasks.json")


def _now() -> float:
    return time.time()


@dataclass
class Task:
    id: str = field(default_factory=lambda: f"task_{uuid.uuid4().hex[:10]}")
    name: str = ""
    commands: str = ""                 # 任务指令（文本）或含 <<taskjs>> 块
    interval: int = 0                  # 0 = 手动触发；N = 每 N 轮触发
    trigger_timing: str = "after_ai"   # after_ai | after_user | before_ai
    floor_type: str = "all"            # all | user | llm
    scope: str = "global"              # global | character | preset
    disabled: bool = False
    button_activated: bool = False
    created_at: float = field(default_factory=_now)
    last_run: float = 0.0
    run_count: int = 0

    def __post_init__(self) -> None:
        self.name = str(self.name or "").strip()
        self.trigger_timing = self.trigger_timing if self.trigger_timing in VALID_TIMING else "after_ai"
        self.floor_type = self.floor_type if self.floor_type in VALID_FLOOR else "all"
        self.scope = self.scope if self.scope in VALID_SCOPE else "global"
        try:
            self.interval = max(0, int(self.interval))
        except (TypeError, ValueError):
            self.interval = 0

    @property
    def manual(self) -> bool:
        return self.interval == 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Task":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        data = {k: v for k, v in (raw or {}).items() if k in known}
        for num, default in (("interval", 0), ("last_run", 0.0), ("run_count", 0)):
            try:
                data[num] = float(data[num]) if num != "interval" and num != "run_count" else int(data.get(num) or default)
            except (TypeError, ValueError):
                data[num] = default
        return cls(**data)

    def has_js_block(self) -> bool:
        return "<<taskjs>>" in str(self.commands or "")

    def strip_js_blocks(self) -> str:
        """去掉 <<taskjs>> 块，只留可注入的文本指令。"""
        s = str(self.commands or "")
        out, i = [], 0
        while True:
            a = s.find("<<taskjs>>", i)
            if a < 0:
                out.append(s[i:]); break
            out.append(s[i:a])
            b = s.find("<</taskjs>>", a)
            i = len(s) if b < 0 else b + len("<</taskjs>>")
        return "".join(out).strip()


class TaskStore:
    """任务持久化（按 scope 分组），原子写盘 + 线程锁。"""

    def __init__(self, path: Path | str = TASKS_FILE):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.buckets: Dict[str, List[Dict[str, Any]]] = {s: [] for s in VALID_SCOPE}
        self.load()

    def load(self) -> None:
        with self._lock:
            self.buckets = {s: [] for s in VALID_SCOPE}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except Exception:
                    raw = {}
                if isinstance(raw, dict):
                    for s in VALID_SCOPE:
                        rows = raw.get(s)
                        if isinstance(rows, list):
                            self.buckets[s] = [Task.from_dict(r).to_dict() for r in rows if isinstance(r, dict)]

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.buckets, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    def list(self, scope: str = "all") -> List[Dict[str, Any]]:
        if scope in VALID_SCOPE:
            return list(self.buckets.get(scope, []))
        out: List[Dict[str, Any]] = []
        for s in VALID_SCOPE:
            out.extend(self.buckets.get(s, []))
        return out

    def find(self, name: str, scope: str = "all") -> Optional[Dict[str, Any]]:
        key = str(name or "").strip().lower()
        for t in self.list(scope):
            if str(t.get("name") or "").strip().lower() == key:
                return t
        return None

    def add(self, task: Task | Dict[str, Any], scope: Optional[str] = None) -> Dict[str, Any]:
        data = task.to_dict() if isinstance(task, Task) else Task.from_dict(task).to_dict()
        sc = scope or data.get("scope") or "global"
        sc = sc if sc in VALID_SCOPE else "global"
        data["scope"] = sc
        with self._lock:
            self.buckets[sc].append(data)
            self.save()
        return data

    def update(self, name: str, scope: str = "all", **fields) -> Optional[Dict[str, Any]]:
        key = str(name or "").strip().lower()
        with self._lock:
            for sc in (VALID_SCOPE if scope == "all" else (scope,)):
                for t in self.buckets.get(sc, []):
                    if str(t.get("name") or "").strip().lower() == key:
                        for k, v in fields.items():
                            if k in Task.__dataclass_fields__:  # type: ignore[attr-defined]
                                t[k] = v
                        fixed = Task.from_dict(t).to_dict()
                        t.clear(); t.update(fixed)
                        self.save()
                        return t
        return None

    def remove(self, name: str, scope: str = "all") -> bool:
        key = str(name or "").strip().lower()
        with self._lock:
            for sc in (VALID_SCOPE if scope == "all" else (scope,)):
                rows = self.buckets.get(sc, [])
                for i, t in enumerate(rows):
                    if str(t.get("name") or "").strip().lower() == key:
                        rows.pop(i)
                        self.save()
                        return True
        return False

    def clear(self, scope: str = "all") -> int:
        with self._lock:
            n = 0
            for sc in (VALID_SCOPE if scope == "all" else (scope,)):
                n += len(self.buckets.get(sc, []))
                self.buckets[sc] = []
            self.save()
            return n


class TaskScheduler:
    """任务调度：楼层计数 → 触发判定 → 单实例锁 + 冷却 → 执行。"""

    def __init__(self, store: TaskStore,
                 cooldown: float = TASK_COOLDOWN,
                 handlers: Optional[Dict[str, Callable[[Task, Dict[str, Any]], Any]]] = None):
        self.store = store
        self.cooldown = float(cooldown)
        self.handlers = dict(handlers or {})
        self._running: Dict[str, float] = {}          # 任务名 -> 开始时间
        self._processed: set = set()                  # 已处理消息 key
        self._last_run: Dict[str, float] = {}
        self._lock = threading.RLock()

    # ---------- 楼层与轮次 ----------
    @staticmethod
    def floor_counts(messages: Sequence[Any]) -> Dict[str, int]:
        """统计 user / llm / total 楼层数。"""
        user = llm = 0
        for m in messages or ():
            role = ""
            if isinstance(m, dict):
                role = str(m.get("role") or m.get("type") or "")
                if not role:
                    role = "user" if m.get("is_user") else "llm"
            else:
                role = str(getattr(m, "role", "") or "")
            if role in ("user", "human"):
                user += 1
            else:
                llm += 1
        return {"user": user, "llm": llm, "total": user + llm}

    @staticmethod
    def pick_floor(floor_type: str, counts: Dict[str, int]) -> int:
        """取当前楼层号（0 基）。"""
        ft = floor_type if floor_type in VALID_FLOOR else "all"
        if ft == "user":
            return max(0, int(counts.get("user", 0)) - 1)
        if ft == "llm":
            return max(0, int(counts.get("llm", 0)) - 1)
        return max(0, int(counts.get("total", 0)) - 1)

    @staticmethod
    def turn_count(counts: Dict[str, int]) -> int:
        """以「一轮 = 用户+模型」计。"""
        return min(int(counts.get("user", 0)), int(counts.get("llm", 0)))

    @staticmethod
    def should_skip_by_context(task_timing: str, trigger_context: str) -> bool:
        """触发时机与当前上下文不匹配则跳过。"""
        return (task_timing or "after_ai") != (trigger_context or "after_ai")

    @staticmethod
    def match_interval(task: Dict[str, Any] | Task, counts: Dict[str, int],
                       trigger_context: str = "after_ai") -> bool:
        """interval=0 手动不自动触发；否则按轮次取模。"""
        iv = int((task.interval if isinstance(task, Task) else task.get("interval")) or 0)
        if iv <= 0:
            return False
        turns = TaskScheduler.turn_count(counts)
        if turns <= 0:
            return False
        return turns % iv == 0

    # ---------- 触发 ----------
    def due_tasks(self, counts: Dict[str, int], trigger_context: str = "after_ai",
                  scope: str = "all") -> List[Dict[str, Any]]:
        out = []
        for t in self.store.list(scope):
            if t.get("disabled"):
                continue
            if self.should_skip_by_context(t.get("trigger_timing"), trigger_context):
                continue
            if self.match_interval(t, counts, trigger_context):
                out.append(t)
        return out

    def mark_message_processed(self, key: str) -> bool:
        """返回 True 表示首次处理。"""
        with self._lock:
            if key in self._processed:
                return False
            self._processed.add(key)
            return True

    def is_message_processed(self, key: str) -> bool:
        return key in self._processed

    def clear_processed(self) -> None:
        with self._lock:
            self._processed.clear()

    # ---------- 执行 ----------
    def run(self, task: Dict[str, Any] | Task, context: Optional[Dict[str, Any]] = None,
            force: bool = False) -> Dict[str, Any]:
        """
        单实例 + 冷却保护地执行任务。
        返回 {ok, reason, output}。reason ∈ busy|cooldown|disabled|empty|error|ok
        """
        t = task if isinstance(task, Task) else Task.from_dict(task)
        ctx = dict(context or {})
        key = t.name or t.id
        now = _now()
        with self._lock:
            if t.disabled and not force:
                return {"ok": False, "reason": "disabled", "task": key}
            if key in self._running:
                return {"ok": False, "reason": "busy", "task": key}
            last = self._last_run.get(key, 0.0)
            if not force and last and (now - last) < self.cooldown:
                return {"ok": False, "reason": "cooldown", "task": key,
                        "remaining": round(self.cooldown - (now - last), 3)}
            self._running[key] = now
        try:
            out = self._execute(t, ctx)
            with self._lock:
                self._last_run[key] = _now()
            self.store.update(t.name, "all", last_run=_now(), run_count=int(t.run_count) + 1)
            return {"ok": True, "reason": "ok", "task": key, "output": out}
        except Exception as e:
            return {"ok": False, "reason": "error", "task": key, "error": str(e)}
        finally:
            with self._lock:
                self._running.pop(key, None)

    def _execute(self, t: Task, ctx: Dict[str, Any]) -> Any:
        handler = self.handlers.get(t.name) or self.handlers.get("*")
        if handler:
            return handler(t, ctx)
        text = t.strip_js_blocks()
        if not text and t.has_js_block():
            return ""                      # 只有 JS 块、无 Python 处理器 → 无可执行内容
        return text

    def reset_run(self, task_name: Optional[str] = None) -> int:
        """复位执行状态。"""
        with self._lock:
            if task_name:
                return 1 if self._running.pop(str(task_name).strip(), None) is not None else 0
            n = len(self._running)
            self._running.clear()
            return n

    def cooldown_status(self) -> Dict[str, float]:
        now = _now()
        return {k: max(0.0, round(self.cooldown - (now - v), 3))
                for k, v in self._last_run.items() if (now - v) < self.cooldown}

    def is_any_running(self) -> bool:
        return bool(self._running)

    # ---------- 注入 ----------
    def build_task_prompt(self, counts: Optional[Dict[str, int]] = None,
                          scope: str = "all", max_chars: int = 1200) -> str:
        """把启用中的任务拼成可注入 Prompt 的委托块（去掉 JS 块）。"""
        lines, used = [], 0
        for t in self.store.list(scope):
            if t.get("disabled"):
                continue
            text = Task.from_dict(t).strip_js_blocks()
            if not text:
                continue
            iv = int(t.get("interval") or 0)
            head = f"- {t.get('name')}" + ("（每 %d 轮）" % iv if iv > 0 else "（手动）")
            seg = f"{head}：{text}"
            if used + len(seg) > max_chars and lines:
                break
            lines.append(seg)
            used += len(seg)
        if not lines:
            return ""
        return "【当前委托】\n" + "\n".join(lines)
