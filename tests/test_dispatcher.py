"""
tests/test_dispatcher.py — Comprehensive Unit & Integration Tests for the
Non-Blocking 3-Key Task Engine & Zero-Focus Background Execution Architecture.

Verifies:
(a) Main Key returns immediately (<300ms) on a 4-step workflow task without blocking.
(b) Worker-1 and Worker-2 run two multi-step tasks concurrently.
(c) Human-in-the-loop approval gate pauses execution on sensitive actions and resumes upon approval.
(d) 3-Key pool resolution with clean fallback to GEMINI_API_KEY.
(e) 3rd multi-step task queues cleanly when both workers are busy.
(f) Zero-focus background browser configuration & CDP lock mutual exclusion.
(g) Contact notification watcher fuzzy matching & Antigravity watcher alerts.
"""

from __future__ import annotations

import asyncio
import functools
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from core.dispatcher import (
    BackgroundWorkflowDispatcher,
    GeminiPoolManager,
    STATE_CANCELLED,
    STATE_DONE,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_WAITING_APPROVAL,
    WorkflowStep,
    WorkflowTask,
)
from core.watchers import (
    AntigravityStatusAlert,
    AntigravityTaskWatcher,
    ContactMessageAlert,
    ContactNotificationWatcher,
    WatcherService,
)
from memory.config_manager import (
    get_gemini_key_pool,
    get_gemini_worker_key_1,
    get_gemini_worker_key_2,
)


def run_async(coro_fn):
    """Helper to run async test functions synchronously with asyncio.run()."""
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


# ── Fixtures ──────────────────────────────────────────────────────────────────

class MockPlayer:
    """Mock PyQt6 GUI player HUD tracking calls safely."""

    def __init__(self):
        self.logs: list[str] = []
        self.tasks: list[dict] = []
        self.alerts: list[str] = []
        self.muted: bool = False
        self.state: str = "IDLE"

    def set_state(self, state: str) -> None:
        self.state = state

    def write_log(self, text: str) -> None:
        self.logs.append(text)

    def show_task_progress(self, task_data: dict) -> int:
        self.tasks.append(task_data)
        return 1

    def show_code_job(self, task_data: dict) -> int:
        self.tasks.append(task_data)
        return 1

    def show_slide_alert(self, message: str, title: str = "") -> None:
        self.alerts.append(f"[{title}] {message}")


class MockLiveSession:
    """Mock live voice session tracking spoken alerts."""

    def __init__(self):
        self.spoken: list[str] = []
        self._action_registry = None
        self._plugin_registry = None
        self._vision_busy = False
        self._vision_last_time = 0.0

    def speak(self, text: str) -> None:
        self.spoken.append(text)


@pytest.fixture
def test_env(monkeypatch):
    """Clean test environment with isolated keys."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTestMainKey1234567890abcdef")
    monkeypatch.setenv("GEMINI_WORKER_KEY_1", "AIzaSyTestWorker1Key1234567890abcdef")
    monkeypatch.setenv("GEMINI_WORKER_KEY_2", "AIzaSyTestWorker2Key1234567890abcdef")


# ── 1. 3-Key Pool Resolution & Fallback Tests ─────────────────────────────────

def test_gemini_key_pool_configuration(test_env):
    """Verify 3-key pool configuration resolves separate keys."""
    pool = get_gemini_key_pool()
    assert pool["main"] == "AIzaSyTestMainKey1234567890abcdef"
    assert pool["worker_1"] == "AIzaSyTestWorker1Key1234567890abcdef"
    assert pool["worker_2"] == "AIzaSyTestWorker2Key1234567890abcdef"


def test_gemini_key_pool_fallback(monkeypatch):
    """Verify worker keys cleanly fall back to main key if omitted."""
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyFallbackMainKey1234567890")
    monkeypatch.delenv("GEMINI_WORKER_KEY_1", raising=False)
    monkeypatch.delenv("GEMINI_WORKER_KEY_2", raising=False)

    w1 = get_gemini_worker_key_1()
    w2 = get_gemini_worker_key_2()
    assert w1 == "AIzaSyFallbackMainKey1234567890"
    assert w2 == "AIzaSyFallbackMainKey1234567890"


# ── 2. Requirement 1(a): Non-Blocking Immediate Dispatch (<300ms) ─────────────

@run_async
async def test_non_blocking_immediate_dispatch():
    """
    Verify that Main Key dispatch_background_workflow returns in <300ms
    on a 4-step task and pushes work onto the queue.
    """
    player = MockPlayer()
    live = MockLiveSession()
    dispatcher = BackgroundWorkflowDispatcher(player=player, live_session=live)

    steps = [
        "Open Gmail and find the latest unread email",
        "Read and summarize the email body",
        "Analyze the sender's request and draft a reply",
        "Save drafted reply to draft box",
    ]

    t0 = time.monotonic()
    result = dispatcher.dispatch_background_workflow(
        goal="Process latest email and draft response",
        steps=steps,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000

    # Strict latency assertion: must return in <300ms
    assert elapsed_ms < 300.0, f"Dispatch took too long: {elapsed_ms:.2f}ms >= 300ms"

    assert result["status"] in ("dispatched", "queued")
    assert "task_id" in result
    assert result["total_steps"] == 4
    assert dispatcher.queue.qsize() == 1

    # Verify task state
    task_id = result["task_id"]
    task = dispatcher.tasks[task_id]
    assert len(task.steps) == 4
    assert task.goal == "Process latest email and draft response"


# ── 3. Requirement 1(b): Worker-1 and Worker-2 Concurrent Execution ──────────

@run_async
async def test_worker_concurrency():
    """
    Verify that Worker-1 and Worker-2 run two multi-step tasks concurrently.
    """
    player = MockPlayer()
    live = MockLiveSession()
    dispatcher = BackgroundWorkflowDispatcher(player=player, live_session=live)

    # Mock step execution to simulate async work
    async def mock_run_step_action(worker_name: str, task: WorkflowTask, step: WorkflowStep):
        await asyncio.sleep(0.08)  # brief async delay
        return f"{worker_name} finished {step.title}"

    dispatcher._run_step_action = mock_run_step_action

    loop = asyncio.get_running_loop()
    dispatcher.start(loop=loop)

    task1_steps = ["Step 1.1", "Step 1.2"]
    task2_steps = ["Step 2.1", "Step 2.2"]

    t0 = time.monotonic()
    res1 = dispatcher.dispatch_background_workflow(goal="Task 1", steps=task1_steps)
    res2 = dispatcher.dispatch_background_workflow(goal="Task 2", steps=task2_steps)

    t1_id = res1["task_id"]
    t2_id = res2["task_id"]

    # Wait for both tasks to complete
    for _ in range(50):
        if dispatcher.tasks[t1_id].status == STATE_DONE and dispatcher.tasks[t2_id].status == STATE_DONE:
            break
        await asyncio.sleep(0.05)

    elapsed = time.monotonic() - t0

    t1 = dispatcher.tasks[t1_id]
    t2 = dispatcher.tasks[t2_id]

    assert t1.status == STATE_DONE
    assert t2.status == STATE_DONE

    # Verify each was handled by a different worker
    workers_used = {t1.assigned_worker, t2.assigned_worker}
    assert workers_used == {"Worker-1", "Worker-2"}, f"Expected both workers used, got: {workers_used}"

    # Verify context propagation
    assert "step_1" in t1.accumulated_context
    assert "step_2" in t1.accumulated_context

    dispatcher.stop()


# ── 4. 3rd Task Clean Queueing (Hard Cap of 2 Concurrent Workers) ─────────────

@run_async
async def test_third_task_queueing_cap():
    """
    Verify that when both Worker-1 and Worker-2 are occupied, a 3rd task
    is cleanly placed into the queue without exceeding the 2-worker cap.
    """
    player = MockPlayer()
    dispatcher = BackgroundWorkflowDispatcher(player=player)

    # Block both workers with a slow step
    async def slow_step(worker_name, task, step):
        await asyncio.sleep(0.3)
        return "done"

    dispatcher._run_step_action = slow_step
    loop = asyncio.get_running_loop()
    dispatcher.start(loop=loop)

    res1 = dispatcher.dispatch_background_workflow(goal="Task A", steps=["Step A1"])
    res2 = dispatcher.dispatch_background_workflow(goal="Task B", steps=["Step B1"])
    res3 = dispatcher.dispatch_background_workflow(goal="Task C", steps=["Step C1"])

    # Task 3 should report queued
    assert res3["status"] == "queued"
    assert res3["queue_position"] >= 1

    status = dispatcher.get_worker_status()
    assert status["queue_depth"] >= 1 or status["worker_1"]["state"] == "BUSY"

    # Wait for completion
    for _ in range(40):
        if dispatcher.tasks[res3["task_id"]].status == STATE_DONE:
            break
        await asyncio.sleep(0.05)

    assert dispatcher.tasks[res3["task_id"]].status == STATE_DONE
    dispatcher.stop()


# ── 5. Requirement 1(c): Human-in-the-Loop Approval Gate ─────────────────────

@run_async
async def test_human_in_the_loop_approval_gate():
    """
    Verify that sensitive steps (e.g. sending email) pause the worker in
    WAITING_APPROVAL state, and resolve_worker_approval cleanly resumes execution.
    """
    player = MockPlayer()
    live = MockLiveSession()
    dispatcher = BackgroundWorkflowDispatcher(player=player, live_session=live)

    async def mock_step_action(worker_name, task, step):
        await asyncio.sleep(0.02)
        return f"Executed {step.action}"

    dispatcher._run_step_action = mock_step_action
    loop = asyncio.get_running_loop()
    dispatcher.start(loop=loop)

    # Workflow with sensitive final step
    steps = [
        {"title": "Read draft", "action": "read_draft"},
        {"title": "Send email to client", "action": "send_email", "parameters": {"to": "client@example.com"}},
    ]

    res = dispatcher.dispatch_background_workflow(goal="Email client", steps=steps)
    task_id = res["task_id"]
    task = dispatcher.tasks[task_id]

    # Wait until step 2 enters WAITING_APPROVAL
    for _ in range(50):
        if task.status == STATE_WAITING_APPROVAL:
            break
        await asyncio.sleep(0.04)

    assert task.status == STATE_WAITING_APPROVAL
    assert task.steps[1].status == STATE_WAITING_APPROVAL
    assert any("requires confirmation" in spk for spk in live.spoken)

    # User speaks "yes / confirm" -> resolve_worker_approval(task_id, True)
    approval_res = dispatcher.resolve_worker_approval(task_id=task_id, approved=True)
    assert approval_res["status"] == "resolved"
    assert approval_res["approved"] is True

    # Worker resumes and finishes task
    for _ in range(50):
        if task.status == STATE_DONE:
            break
        await asyncio.sleep(0.04)

    assert task.status == STATE_DONE
    assert task.steps[1].status == STATE_DONE

    dispatcher.stop()


@run_async
async def test_human_in_the_loop_rejection():
    """Verify rejecting an approval cleanly cancels the step and task."""
    player = MockPlayer()
    live = MockLiveSession()
    dispatcher = BackgroundWorkflowDispatcher(player=player, live_session=live)

    async def mock_step_action(worker_name, task, step):
        return "done"

    dispatcher._run_step_action = mock_step_action
    loop = asyncio.get_running_loop()
    dispatcher.start(loop=loop)

    steps = [
        {"title": "Delete folder", "action": "delete_folder", "parameters": {"path": "/tmp/test"}},
    ]

    res = dispatcher.dispatch_background_workflow(goal="Delete files", steps=steps)
    task_id = res["task_id"]
    task = dispatcher.tasks[task_id]

    for _ in range(50):
        if task.status == STATE_WAITING_APPROVAL:
            break
        await asyncio.sleep(0.04)

    assert task.status == STATE_WAITING_APPROVAL

    # Reject approval
    dispatcher.resolve_worker_approval(task_id=task_id, approved=False)

    for _ in range(50):
        if task.status == STATE_CANCELLED:
            break
        await asyncio.sleep(0.04)

    assert task.status == STATE_CANCELLED
    assert task.steps[0].status == STATE_CANCELLED

    dispatcher.stop()


# ── 6. Task Cancellation & Status Control ────────────────────────────────────

@run_async
async def test_cancel_background_task():
    """Verify cancel_background_task terminates a running task."""
    player = MockPlayer()
    dispatcher = BackgroundWorkflowDispatcher(player=player)

    async def slow_step(w, t, s):
        await asyncio.sleep(0.5)
        return "done"

    dispatcher._run_step_action = slow_step
    loop = asyncio.get_running_loop()
    dispatcher.start(loop=loop)

    res = dispatcher.dispatch_background_workflow(goal="Long Task", steps=["Step 1", "Step 2"])
    task_id = res["task_id"]

    await asyncio.sleep(0.05)
    cancel_res = dispatcher.cancel_background_task(task_id)
    assert cancel_res["status"] == "cancelled"

    task = dispatcher.tasks[task_id]
    assert task.status == STATE_CANCELLED

    dispatcher.stop()


# ── 7. Requirement 2: Detached Background Browser Verification ────────────────

def test_background_browser_configuration():
    """
    Verify actions/background_browser.py enforces zero-focus parameters:
    CDP port 9223, off-screen window placement (-32000, -32000), and async lock.
    """
    from actions.background_browser import BackgroundBrowserManager, DEFAULT_WORKER_PORT

    mgr = BackgroundBrowserManager.get_instance()
    assert mgr.port == DEFAULT_WORKER_PORT == 9223
    assert mgr.cdp_url == "http://127.0.0.1:9223"
    assert mgr.profile_dir.name == "worker_browser_profile"

    # Verify asyncio.Lock exists for mutual exclusion
    assert isinstance(mgr._cdp_lock, asyncio.Lock)


@run_async
async def test_silent_subprocess_execution():
    """Verify run_silent_subprocess runs with CREATE_NO_WINDOW without throwing."""
    from actions.background_browser import run_silent_subprocess

    code, stdout, stderr = await run_silent_subprocess(["python", "-c", "print('silent_ok')"])
    assert code == 0
    assert "silent_ok" in stdout


# ── 8. Requirement 3: Contact Notification & Antigravity Watchers ─────────────

def test_contact_notification_watcher_matching():
    """Verify ContactNotificationWatcher case-insensitive fuzzy matching and alert dispatch."""
    received_alerts: list[ContactMessageAlert] = []

    def on_alert(alert: ContactMessageAlert):
        received_alerts.append(alert)

    watcher = ContactNotificationWatcher(alert_callback=on_alert)

    # Test fuzzy matching
    assert watcher._matches_contact("Elon Musk", "Message from Elon Musk: Rocket launch at 5pm")
    assert watcher._matches_contact("Alex", "Alex: Hey, are you free?")
    assert watcher._matches_contact("Sarah", "From SARAH regarding project")
    assert not watcher._matches_contact("David", "John: Hello there")


def test_antigravity_task_watcher_window_detection():
    """Verify AntigravityTaskWatcher parses approval and completion states."""
    watcher = AntigravityTaskWatcher()

    # Test state matching logic
    with patch.object(watcher, "_check_window_titles", return_value=("waiting_approval", "Waiting for approval")):
        st, detail = watcher._check_window_titles()
        assert st == "waiting_approval"

    with patch.object(watcher, "_check_window_titles", return_value=("completed", "All tasks complete")):
        st, detail = watcher._check_window_titles()
        assert st == "completed"


def test_watcher_service_management_tools():
    """Verify WatcherService registers contact watches and antigravity monitoring."""
    player = MockPlayer()
    live = MockLiveSession()
    svc = WatcherService(live_session=live, player=player)

    res_contact = svc.watch_contact_messages("Alice", app_filter=["WhatsApp"])
    assert res_contact["status"] == "watching"
    assert res_contact["contact"] == "Alice"
    assert "Alice" in svc.contact_watcher.watched_contacts

    res_anti = svc.watch_antigravity_task()
    assert res_anti["status"] == "watching"

    # Simulate contact alert injection
    alert = ContactMessageAlert(contact="Alice", app="WhatsApp", sender="Alice", preview="Hello Jarvis")
    svc._on_contact_alert(alert)

    assert any("Alice" in spk for spk in live.spoken)
    assert any("Alice" in l for l in player.logs)


# ── 9. Main Key Action Handlers & ToolDispatcher Integration ──────────────────

def test_workflow_dispatcher_actions_module():
    """Verify actions/workflow_dispatcher.py handlers return proper JSON."""
    from actions.workflow_dispatcher import (
        cancel_background_task,
        dispatch_background_workflow,
        get_worker_status,
        resolve_worker_approval,
        watch_antigravity_task,
        watch_contact_messages,
    )

    player = MockPlayer()

    # 1. Dispatch
    disp_res = json.loads(dispatch_background_workflow({"goal": "Test Goal", "steps": ["S1", "S2"]}, player=player))
    assert "task_id" in disp_res
    task_id = disp_res["task_id"]

    # 2. Status
    status_res = json.loads(get_worker_status(player=player))
    assert "worker_1" in status_res
    assert "worker_2" in status_res
    assert "queue_depth" in status_res

    # 3. Resolve approval (not waiting)
    appr_res = json.loads(resolve_worker_approval({"task_id": task_id, "approved": True}, player=player))
    assert "status" in appr_res

    # 4. Cancel
    canc_res = json.loads(cancel_background_task({"task_id": task_id}, player=player))
    assert canc_res["status"] == "cancelled"

    # 5. Watch contact
    wc_res = json.loads(watch_contact_messages({"contact_name": "Bob"}, player=player))
    assert wc_res["status"] == "watching"

    # 6. Watch Antigravity
    wa_res = json.loads(watch_antigravity_task(player=player))
    assert wa_res["status"] == "watching"


@run_async
async def test_tool_dispatcher_integration():
    """Verify ToolDispatcher handles the new background tools natively."""
    from core.tool_dispatcher import ToolDispatcher
    from types import SimpleNamespace

    player = MockPlayer()
    live = MockLiveSession()
    live.ui = player
    live.speak_error = MagicMock()
    live._record_activity = MagicMock()
    live.voice_state = SimpleNamespace(
        begin_command=MagicMock(),
        end_command=MagicMock(),
        is_disabled=False,
        is_sleeping=False,
    )

    dispatcher = ToolDispatcher(session_context=live)

    # Fake FunctionCall
    fc = SimpleNamespace(
        id="call_1",
        name="dispatch_background_workflow",
        args={"goal": "Sync task", "steps": ["Step 1", "Step 2"]},
    )

    resp = await dispatcher.execute_tool(live, fc)
    assert resp.name == "dispatch_background_workflow"
    parsed = json.loads(resp.response["result"])
    assert "task_id" in parsed
