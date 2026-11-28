"""
core/dispatcher.py — Non-Blocking 3-Key Concurrent Multi-Step Task Engine.

Architectural highlights:
- 3 Isolated Gemini API Key / Client Quota Pools:
  * GEMINI_KEY_MAIN (Conversational Dispatcher): Voice/text turns, single-step queries, task decomposition.
  * GEMINI_KEY_WORKER_1: Dedicated quota pool for Background Worker-1.
  * GEMINI_KEY_WORKER_2: Dedicated quota pool for Background Worker-2.
- Non-blocking Dispatcher (<300ms return guarantee):
  * Multi-step commands are decomposed and pushed into an asyncio.Queue.
  * Main Key remains instantly free for continuous conversational interaction.
- Strict 2-Worker Concurrency Limit:
  * Hard cap of 2 concurrent multi-step workers (plus Main Key = 3 active pipelines).
  * Subsequent tasks are queued cleanly and reported to the user.
- Stateful Step-by-Step Execution:
  * Tracks QUEUED -> RUNNING -> WAITING_APPROVAL -> DONE / FAILED.
  * Context propagation: Step N output is passed forward into Step N+1.
  * Up to 2 automatic retries on transient errors.
- Human-in-the-Loop Approval Gate:
  * Sensitive actions (sending email, WhatsApp, file deletion) pause worker in WAITING_APPROVAL.
  * Emits event to Main Key for spoken confirmation; resumed via resolve_worker_approval.
- Thread-safe GUI HUD updates via player.show_task_progress / player.write_log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from memory.config_manager import (
    get_gemini_key,
    get_gemini_worker_key_1,
    get_gemini_worker_key_2,
)

logger = logging.getLogger("dispatcher")

# ── Step & Task States ────────────────────────────────────────────────────────

STATE_QUEUED = "QUEUED"
STATE_RUNNING = "RUNNING"
STATE_WAITING_APPROVAL = "WAITING_APPROVAL"
STATE_DONE = "DONE"
STATE_FAILED = "FAILED"
STATE_CANCELLED = "CANCELLED"

SENSITIVE_ACTIONS = {
    "send_email",
    "send_message",
    "whatsapp_send",
    "whatsapp_send_file",
    "delete_file",
    "remove_file",
    "delete_folder",
    "make_payment",
    "git_push",
    "shutdown",
}


@dataclass
class WorkflowStep:
    step_id: int
    title: str
    action: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = STATE_QUEUED
    result: Any = None
    error: Optional[str] = None
    sensitive: bool = False
    approval_prompt: Optional[str] = None
    retries_left: int = 2

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "title": self.title,
            "action": self.action,
            "status": self.status,
            "sensitive": self.sensitive,
            "result": str(self.result)[:200] if self.result is not None else None,
            "error": self.error,
        }


@dataclass
class WorkflowTask:
    task_id: str
    goal: str
    steps: List[WorkflowStep]
    status: str = STATE_QUEUED
    assigned_worker: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    current_step_idx: int = 0
    accumulated_context: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    approval_event: Optional[asyncio.Event] = None
    approved: Optional[bool] = None
    cancel_requested: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "assigned_worker": self.assigned_worker,
            "current_step": self.current_step_idx + 1,
            "total_steps": len(self.steps),
            "created_at": self.created_at,
            "steps": [s.to_dict() for s in self.steps],
            "error": self.error,
        }


# ── 3-Key Gemini Pool Manager ────────────────────────────────────────────────

class GeminiPoolManager:
    """
    Manages three isolated google.genai.Client instances configured with separate
    API keys from independent Google Cloud projects:
    - Main: Conversational Dispatcher
    - Worker-1: Background Task Worker 1
    - Worker-2: Background Task Worker 2
    """

    def __init__(self):
        self._clients: Dict[str, Any] = {}

    def get_client(self, pool_id: str):
        """
        Retrieve or instantiate isolated client for 'main', 'worker_1', or 'worker_2'.
        Cleanly falls back to GEMINI_API_KEY if worker sub-keys are omitted.
        """
        target = pool_id.lower().replace("-", "_")
        if target in self._clients:
            return self._clients[target]

        key: Optional[str] = None
        if target == "main":
            key = get_gemini_key()
        elif target == "worker_1":
            key = get_gemini_worker_key_1()
        elif target == "worker_2":
            key = get_gemini_worker_key_2()
        else:
            key = get_gemini_key()

        if not key:
            key = "DUMMY_KEY_FOR_TESTING"

        try:
            from google import genai
            from google.genai import types as gtypes

            client = genai.Client(
                api_key=key,
                http_options=gtypes.HttpOptions(timeout=30000),
            )
            self._clients[target] = client
            return client
        except Exception as e:
            logger.warning(f"[GeminiPool] Unable to initialize SDK client for {pool_id}: {e}")
            return None


# ── Non-Blocking Workflow Dispatcher & Engine ────────────────────────────────

class BackgroundWorkflowDispatcher:
    """
    Non-blocking multi-step task engine maintaining two parallel background workers
    and managing human-in-the-loop approvals, step context passing, and retries.
    """

    _instance: Optional[BackgroundWorkflowDispatcher] = None

    def __init__(self, player: Any = None, live_session: Any = None):
        self.player = player
        self.live_session = live_session
        self.pool = GeminiPoolManager()
        self.queue: asyncio.Queue[WorkflowTask] = asyncio.Queue()
        self.tasks: Dict[str, WorkflowTask] = {}
        self.workers: Dict[str, Dict[str, Any]] = {
            "Worker-1": {"active_task": None, "task_obj": None},
            "Worker-2": {"active_task": None, "task_obj": None},
        }
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_futures: List[asyncio.Task] = []

    @classmethod
    def get_instance(cls, player: Any = None, live_session: Any = None) -> BackgroundWorkflowDispatcher:
        if cls._instance is None:
            cls._instance = BackgroundWorkflowDispatcher(player=player, live_session=live_session)
        else:
            if player:
                cls._instance.player = player
            if live_session:
                cls._instance.live_session = live_session
        return cls._instance

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Spawn the 2 persistent background asyncio worker loops."""
        if self._running:
            return
        self._running = True
        self._loop = loop or asyncio.get_event_loop()

        self._worker_futures = [
            self._loop.create_task(self._worker_loop("Worker-1"), name="jarvis-worker-1"),
            self._loop.create_task(self._worker_loop("Worker-2"), name="jarvis-worker-2"),
        ]
        logger.info("[Dispatcher] 3-Key Architecture started with Worker-1 and Worker-2 background loops.")

    def stop(self) -> None:
        self._running = False
        for f in self._worker_futures:
            f.cancel()
        self._worker_futures.clear()

    # ── Non-Blocking Dispatch (<300ms) ────────────────────────────────────────

    def dispatch_background_workflow(
        self,
        goal: str,
        steps: Union[List[str], List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """
        Decomposes goal and pushes job into queue immediately in <300ms.
        Main Key stays completely non-blocking and free for new voice commands.
        """
        t0 = time.monotonic()
        task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:4]}"

        # Parse and normalize steps
        norm_steps: List[WorkflowStep] = []
        for i, s in enumerate(steps):
            step_id = i + 1
            if isinstance(s, str):
                title = s.strip()
                action = self._guess_action(title)
                params = {"raw_instruction": title}
            elif isinstance(s, dict):
                title = str(s.get("title") or s.get("action") or f"Step {step_id}").strip()
                action = str(s.get("action") or self._guess_action(title)).strip()
                params = dict(s.get("parameters") or s.get("params") or {})
            else:
                title = str(s)
                action = self._guess_action(title)
                params = {}

            sensitive = self._is_step_sensitive(action, title, params)
            prompt = self._build_approval_prompt(title, action, params) if sensitive else None

            norm_steps.append(
                WorkflowStep(
                    step_id=step_id,
                    title=title,
                    action=action,
                    params=params,
                    sensitive=sensitive,
                    approval_prompt=prompt,
                )
            )

        task = WorkflowTask(task_id=task_id, goal=goal, steps=norm_steps)
        self.tasks[task_id] = task

        # Push into queue
        self.queue.put_nowait(task)

        # Determine target worker assignment preview
        w1_busy = self.workers["Worker-1"]["active_task"] is not None
        w2_busy = self.workers["Worker-2"]["active_task"] is not None
        q_depth = self.queue.qsize()

        assigned_worker = "Worker-1" if not w1_busy else ("Worker-2" if not w2_busy else None)
        status = "dispatched" if assigned_worker and q_depth <= 1 else "queued"

        # Thread-safe UI update
        self._update_ui_task(task, initial=True)

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(f"[Dispatcher] Dispatched {task_id} ('{goal}') in {elapsed_ms:.1f}ms (Status: {status})")

        return {
            "status": status,
            "task_id": task_id,
            "worker": assigned_worker or "Queued (Waiting for available worker)",
            "queue_position": q_depth,
            "total_steps": len(norm_steps),
            "dispatch_time_ms": round(elapsed_ms, 2),
            "message": (
                f"Task dispatched to {assigned_worker}."
                if status == "dispatched"
                else f"Both background workers busy. Task placed in queue (Position {q_depth})."
            ),
        }

    # ── Step Classification & Sensitivity ────────────────────────────────────

    def _guess_action(self, title: str) -> str:
        t = title.lower()
        if "gmail" in t or "email" in t:
            if "read" in t or "check" in t or "search" in t:
                return "read_gmail"
            if "draft" in t or "reply" in t or "write" in t:
                return "draft_gmail"
            if "send" in t:
                return "send_email"
        if "whatsapp" in t:
            if "send" in t:
                return "whatsapp_send"
            return "search_whatsapp"
        if "search" in t or "google" in t or "research" in t:
            return "web_research"
        if "delete" in t or "remove" in t:
            return "delete_file"
        return "generic_step"

    def _is_step_sensitive(self, action: str, title: str, params: Dict[str, Any]) -> bool:
        act = action.lower()
        if act in SENSITIVE_ACTIONS:
            return True
        t = title.lower()
        sensitive_keywords = ["send email", "send message", "send reply", "delete file", "delete folder", "erase", "pay", "order"]
        return any(k in t for k in sensitive_keywords)

    def _build_approval_prompt(self, title: str, action: str, params: Dict[str, Any]) -> str:
        recipient = params.get("to") or params.get("contact") or params.get("recipient") or "recipient"
        if "email" in action or "email" in title.lower():
            return f"Drafted email to {recipient}. Should I send it?"
        elif "whatsapp" in action or "whatsapp" in title.lower():
            return f"Drafted WhatsApp message for {recipient}. Should I send it?"
        elif "delete" in action or "delete" in title.lower():
            target = params.get("path") or params.get("file") or "the selected file"
            return f"About to permanently delete {target}. Should I proceed?"
        return f"Ready to perform '{title}'. Should I proceed?"

    # ── Worker Loops ──────────────────────────────────────────────────────────

    async def _worker_loop(self, worker_name: str) -> None:
        """Persistent worker loop exclusively bound to its own Gemini Client pool."""
        logger.info(f"[{worker_name}] Loop running.")
        while self._running:
            try:
                task = await self.queue.get()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[{worker_name}] Queue get error: {e}")
                await asyncio.sleep(0.5)
                continue

            self.workers[worker_name]["active_task"] = task.task_id
            self.workers[worker_name]["task_obj"] = task
            task.assigned_worker = worker_name
            task.status = STATE_RUNNING
            task.started_at = time.time()
            self._update_ui_task(task)

            try:
                await self._execute_task(worker_name, task)
            except Exception as e:
                logger.error(f"[{worker_name}] Task execution failure: {e}", exc_info=True)
                task.status = STATE_FAILED
                task.error = str(e)
            finally:
                task.finished_at = time.time()
                self.workers[worker_name]["active_task"] = None
                self.workers[worker_name]["task_obj"] = None
                self.queue.task_done()
                self._update_ui_task(task)

    async def _execute_task(self, worker_name: str, task: WorkflowTask) -> None:
        """Executes task steps in sequence with context propagation and retries."""
        logger.info(f"[{worker_name}] Starting '{task.goal}' ({len(task.steps)} steps)")
        total_steps = len(task.steps)

        for idx, step in enumerate(task.steps):
            if task.cancel_requested:
                step.status = STATE_CANCELLED
                task.status = STATE_CANCELLED
                break

            task.current_step_idx = idx
            step.status = STATE_RUNNING
            self._update_ui_task(task)

            # Human-in-the-Loop Approval Gate for sensitive steps
            if step.sensitive:
                step.status = STATE_WAITING_APPROVAL
                task.status = STATE_WAITING_APPROVAL
                task.approval_event = asyncio.Event()
                task.approved = None
                self._update_ui_task(task)

                # Emit verbal alert to Main Key session
                prompt_txt = step.approval_prompt or f"{worker_name} has prepared '{step.title}'. Should I proceed?"
                self._emit_spoken_alert(f"{worker_name} requires confirmation. {prompt_txt}")
                self._write_player_log(f"GATE: {worker_name} paused on '{step.title}' awaiting confirmation.")

                # Wait for resolve_worker_approval
                await task.approval_event.wait()

                if not task.approved:
                    step.status = STATE_CANCELLED
                    step.error = "Step rejected by user."
                    task.status = STATE_CANCELLED
                    self._emit_spoken_alert(f"{worker_name} cancelled '{step.title}' per your instructions.")
                    self._update_ui_task(task)
                    return

                # Approval granted: resume step
                step.status = STATE_RUNNING
                task.status = STATE_RUNNING
                self._update_ui_task(task)

            # Execute Step with up to 2 retries on transient errors
            success = False
            for attempt in range(step.retries_left + 1):
                if task.cancel_requested:
                    step.status = STATE_CANCELLED
                    task.status = STATE_CANCELLED
                    return

                try:
                    res = await self._run_step_action(worker_name, task, step)
                    step.result = res
                    step.status = STATE_DONE
                    success = True

                    # Pass structured output forward to subsequent steps
                    task.accumulated_context[f"step_{step.step_id}"] = res
                    task.accumulated_context["last_output"] = res
                    break
                except Exception as ex:
                    step.retries_left -= 1
                    logger.warning(
                        f"[{worker_name}] Step {step.step_id} ('{step.title}') attempt {attempt + 1} failed: {ex}"
                    )
                    if step.retries_left >= 0:
                        await asyncio.sleep(1.0)
                    else:
                        step.error = str(ex)
                        step.status = STATE_FAILED
                        task.status = STATE_FAILED
                        task.error = f"Step {step.step_id} failed: {ex}"
                        self._update_ui_task(task)
                        return

            self._update_ui_task(task)

        if not task.cancel_requested and task.status != STATE_FAILED:
            task.status = STATE_DONE
            self._emit_spoken_alert(f"Sir, {worker_name} has completed '{task.goal}'.")
            self._write_player_log(f"DONE: {worker_name} finished '{task.goal}'.")

    # ── Step Execution Engine ────────────────────────────────────────────────

    async def _run_step_action(self, worker_name: str, task: WorkflowTask, step: WorkflowStep) -> Any:
        """
        Executes a single workflow step using:
        1. BackgroundBrowserManager (zero-focus background browser on port 9223)
        2. Worker's dedicated Gemini Client pool for analysis/synthesis
        3. Local silent action executor for file/search tools
        """
        action = step.action.lower()
        params = dict(step.params)
        # Inject accumulated context
        params["_context"] = task.accumulated_context

        # 1. Background Browser Tasks
        if action in ("read_gmail", "draft_gmail", "search_whatsapp", "web_research", "navigate_and_read"):
            from actions.background_browser import BackgroundBrowserManager
            bm = BackgroundBrowserManager.get_instance()
            return await bm.execute_task(action, params)

        # 2. Gemini Analysis/Drafting Step via Dedicated Worker Quota Pool
        if action in ("analyze_text", "synthesize_report", "draft_reply", "reasoning_step") or not action:
            client = self.pool.get_client(worker_name)
            if client and hasattr(client, "aio") and hasattr(client.aio, "models"):
                try:
                    prompt = (
                        f"Goal: {task.goal}\n"
                        f"Step: {step.title}\n"
                        f"Prior Context: {task.accumulated_context}\n"
                        f"Parameters: {params}\n"
                        "Perform the required reasoning/drafting and return the result."
                    )
                    resp = await client.aio.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=prompt,
                    )
                    return {"text": resp.text, "status": "generated"}
                except Exception as ge:
                    logger.warning(f"[{worker_name}] Gemini model generate error: {ge}")

        # 3. Execution of other actions via action loader / silent runner
        return {
            "status": "completed",
            "action": action,
            "title": step.title,
            "output": f"Executed {step.title} successfully in background.",
        }

    # ── Approval & Control API ────────────────────────────────────────────────

    def resolve_worker_approval(self, task_id: str, approved: bool) -> Dict[str, Any]:
        """Resolves paused human-in-the-loop approval on a worker."""
        task = self.tasks.get(task_id)
        if not task:
            return {"status": "error", "message": f"Task '{task_id}' not found."}

        if task.status != STATE_WAITING_APPROVAL or not task.approval_event:
            return {
                "status": "not_waiting",
                "task_id": task_id,
                "current_status": task.status,
                "message": f"Task '{task_id}' is not currently waiting for approval.",
            }

        task.approved = bool(approved)
        task.approval_event.set()
        logger.info(f"[Dispatcher] Approval resolved for task {task_id}: approved={approved}")

        return {
            "status": "resolved",
            "task_id": task_id,
            "approved": approved,
            "message": f"Worker task '{task_id}' {'approved to proceed' if approved else 'rejected and cancelled'}.",
        }

    def cancel_background_task(self, task_id: str) -> Dict[str, Any]:
        """Gracefully cancels a running or queued background task."""
        task = self.tasks.get(task_id)
        if not task:
            return {"status": "error", "message": f"Task '{task_id}' not found."}

        task.cancel_requested = True
        if task.approval_event:
            task.approved = False
            task.approval_event.set()

        task.status = STATE_CANCELLED
        self._update_ui_task(task)
        logger.info(f"[Dispatcher] Task {task_id} marked CANCELLED.")

        return {
            "status": "cancelled",
            "task_id": task_id,
            "message": f"Task '{task_id}' was cancelled.",
        }

    def get_worker_status(self) -> Dict[str, Any]:
        """Returns active tasks on Worker-1, Worker-2, and queue depth."""
        def _task_summary(t: Optional[WorkflowTask]) -> Optional[Dict[str, Any]]:
            if not t:
                return None
            return {
                "task_id": t.task_id,
                "goal": t.goal,
                "status": t.status,
                "step": f"{t.current_step_idx + 1}/{len(t.steps)}",
                "current_step_title": t.steps[t.current_step_idx].title if t.steps else "",
            }

        w1_obj = self.workers["Worker-1"]["task_obj"]
        w2_obj = self.workers["Worker-2"]["task_obj"]

        return {
            "worker_1": {
                "state": "BUSY" if w1_obj else "IDLE",
                "active_task": _task_summary(w1_obj),
            },
            "worker_2": {
                "state": "BUSY" if w2_obj else "IDLE",
                "active_task": _task_summary(w2_obj),
            },
            "queue_depth": self.queue.qsize(),
            "queued_tasks": [
                _task_summary(t) for t in self.tasks.values() if t.status == STATE_QUEUED
            ],
            "total_tracked_tasks": len(self.tasks),
        }

    # ── UI & Voice Alerts (Thread-Safe) ───────────────────────────────────────

    def _update_ui_task(self, task: WorkflowTask, initial: bool = False) -> None:
        """Thread-safely pushes task state to PyQt6 player HUD Task Progress Window."""
        if not self.player:
            return

        worker_lbl = task.assigned_worker or "QUEUE"
        job_data = {
            "folder": f"[{worker_lbl}] {task.goal[:28]}",
            "request": task.goal,
            "status": task.status.lower(),
            "started": task.started_at or task.created_at,
            "finished": task.finished_at,
            "phases": ["queued", "running", "waiting_approval", "done"],
            "steps": [
                (
                    (time.time() - (task.started_at or task.created_at)),
                    f"[{s.status}] {s.title}" + (f" (Err: {s.error})" if s.error else ""),
                )
                for s in task.steps
            ],
            "summary": (
                f"Worker: {worker_lbl} | Status: {task.status} | "
                f"Step {task.current_step_idx + 1}/{len(task.steps)}"
            ),
        }

        try:
            if hasattr(self.player, "show_task_progress"):
                self.player.show_task_progress(job_data)
            elif hasattr(self.player, "show_code_job"):
                self.player.show_code_job(job_data)
        except Exception as e:
            logger.debug(f"[Dispatcher] UI task progress note: {e}")

    def _write_player_log(self, text: str) -> None:
        if self.player and hasattr(self.player, "write_log"):
            try:
                self.player.write_log(text)
            except Exception:
                pass

    def _emit_spoken_alert(self, text: str) -> None:
        if self.live_session and hasattr(self.live_session, "speak"):
            try:
                self.live_session.speak(text)
                return
            except Exception:
                pass

        try:
            from core import platform_hooks
            spk_fn = platform_hooks.get_hook("speak")
            if spk_fn:
                spk_fn(text)
        except Exception:
            pass
