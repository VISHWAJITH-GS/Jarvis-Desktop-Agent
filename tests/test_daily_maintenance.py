"""
tests/test_daily_maintenance.py — Unit tests for scripts/daily_maintenance.py
"""

from __future__ import annotations

from pathlib import Path

from scripts.daily_maintenance import (
    AuditSummary,
    CodeChange,
    MaintenancePlan,
    VerificationNotes,
    apply_maintenance_plan,
    apply_snippet_replacement,
    build_maintenance_context,
    extract_outline,
    format_plan_as_markdown,
    get_git_diff,
    get_git_history,
    select_rotating_files,
    verify_syntax,
)


def test_git_helpers(tmp_path: Path):
    """Git helpers return string output without crashing even on empty/temp dir."""
    hist = get_git_history(tmp_path)
    assert isinstance(hist, str)
    assert len(hist) > 0

    diff = get_git_diff(tmp_path)
    assert isinstance(diff, str)
    assert len(diff) > 0


def test_extract_outline(tmp_path: Path):
    """AST outline extractor returns classes and functions with lines."""
    py_file = tmp_path / "sample.py"
    py_file.write_text(
        "import os\n\n"
        "class HelperService:\n"
        "    def run(self):\n"
        "        pass\n\n"
        "async def process_task(x: int) -> int:\n"
        "    return x * 2\n",
        encoding="utf-8",
    )
    outline = extract_outline(py_file)
    assert "HelperService" in outline
    assert "process_task" in outline


def test_snippet_replacement_exact():
    original = "def add(a, b):\n    return a + b\n"
    target_snippet = "return a + b"
    replacement = "return int(a) + int(b)"

    new_content, success, msg = apply_snippet_replacement(original, target_snippet, replacement)
    assert success is True
    assert "return int(a) + int(b)" in new_content
    assert "Exact match" in msg


def test_snippet_replacement_crlf_normalization():
    original = "def add(a, b):\r\n    return a + b\r\n"
    target_snippet = "return a + b"
    replacement = "return a + b  # hardened"

    new_content, success, msg = apply_snippet_replacement(original, target_snippet, replacement)
    assert success is True
    assert "return a + b  # hardened" in new_content
    assert "\r\n" in new_content


def test_snippet_replacement_whitespace_tolerant():
    original = "    def calc():\n        x = 10\n        y = 20\n        return x + y\n"
    target_snippet = "x = 10\ny = 20\nreturn x + y"
    replacement = "x = 10\ny = 30\nreturn x + y"

    new_content, success, _ = apply_snippet_replacement(original, target_snippet, replacement)
    assert success is True
    assert "y = 30" in new_content


def test_snippet_replacement_failure():
    original = "def calc():\n    return 42\n"
    target_snippet = "return 9999"
    replacement = "return 0"

    new_content, success, msg = apply_snippet_replacement(original, target_snippet, replacement)
    assert success is False
    assert "could not be located" in msg
    assert new_content == original


def test_verify_syntax(tmp_path: Path):
    valid_py = tmp_path / "valid.py"
    valid_py.write_text("def hello(): return 'world'", encoding="utf-8")
    ok, _ = verify_syntax(valid_py, valid_py.read_text())
    assert ok is True

    invalid_py = tmp_path / "invalid.py"
    invalid_content = "def hello() return 'world'"
    ok, err = verify_syntax(invalid_py, invalid_content)
    assert ok is False
    assert "SyntaxError" in err


def test_select_rotating_files_lru(tmp_path: Path):
    f1 = tmp_path / "old.py"
    f2 = tmp_path / "new.py"
    f1.write_text("x = 1", encoding="utf-8")
    f2.write_text("y = 2", encoding="utf-8")

    # Manually adjust mtimes
    import os
    os.utime(f1, (1000, 1000))
    os.utime(f2, (5000, 5000))

    selected = select_rotating_files(tmp_path, [f2, f1], sample_size=1, strategy="lru")
    assert len(selected) == 1
    assert selected[0] == f1


def test_build_maintenance_context(tmp_path: Path):
    f1 = tmp_path / "test_module.py"
    f1.write_text("def test_fn(): pass", encoding="utf-8")

    context = build_maintenance_context(
        repo_root=tmp_path,
        files=[f1],
        git_history="abc1234 test commit",
        git_diff="1 file changed",
        char_budget=10_000,
    )
    assert "abc1234 test commit" in context
    assert "test_module.py" in context
    assert "def test_fn(): pass" in context


def test_format_plan_as_markdown():
    plan = MaintenancePlan(
        audit_summary=AuditSummary(
            focus_area="Async & Live API Resilience",
            target_files=["core/live_session.py"],
            why_this_matters="Prevents silent socket hanging during reconnects.",
        ),
        changes=[
            CodeChange(
                file_path="core/live_session.py",
                original_snippet="ws.connect()",
                replacement_snippet="ws.connect(timeout=10)",
                explanation="Add explicit timeout.",
            )
        ],
        pr_title="fix(live): add timeout to websocket connect",
        pr_body="This adds a 10s timeout to websocket connect.",
        verification_notes=VerificationNotes(
            automated_checks=["python -m py_compile core/live_session.py"],
            human_review_checklist=["Verify reconnect loop does not spin."],
        ),
    )
    md = format_plan_as_markdown(plan)
    assert "## 1. Audit Summary" in md
    assert "## 2. Proposed Changes" in md
    assert "## 3. Verification & PR Notes" in md
    assert "core/live_session.py" in md
    assert "fix(live): add timeout to websocket connect" in md


def test_apply_maintenance_plan_dry_run(tmp_path: Path):
    f = tmp_path / "sample.py"
    f.write_text("def run():\n    return False\n", encoding="utf-8")

    plan = MaintenancePlan(
        audit_summary=AuditSummary(
            focus_area="Quality",
            target_files=["sample.py"],
            why_this_matters="Fix return value",
        ),
        changes=[
            CodeChange(
                file_path="sample.py",
                original_snippet="return False",
                replacement_snippet="return True",
                explanation="Change return value",
            )
        ],
        pr_title="fix: sample",
        pr_body="sample body",
        verification_notes=VerificationNotes(
            automated_checks=["py_compile"],
            human_review_checklist=["Check"],
        ),
    )

    success, modified, errors = apply_maintenance_plan(tmp_path, plan, dry_run=True)
    assert success is True
    assert len(errors) == 0
    # Dry run should NOT write to disk
    assert "return False" in f.read_text(encoding="utf-8")


def test_apply_maintenance_plan_real_apply(tmp_path: Path):
    f = tmp_path / "sample.py"
    f.write_text("def run():\n    return False\n", encoding="utf-8")

    plan = MaintenancePlan(
        audit_summary=AuditSummary(
            focus_area="Quality",
            target_files=["sample.py"],
            why_this_matters="Fix return value",
        ),
        changes=[
            CodeChange(
                file_path="sample.py",
                original_snippet="return False",
                replacement_snippet="return True",
                explanation="Change return value",
            )
        ],
        pr_title="fix: sample",
        pr_body="sample body",
        verification_notes=VerificationNotes(
            automated_checks=["py_compile"],
            human_review_checklist=["Check"],
        ),
    )

    success, modified, errors = apply_maintenance_plan(tmp_path, plan, dry_run=False)
    assert success is True
    assert len(errors) == 0
    assert "sample.py" in modified
    # File on disk should now be updated
    assert "return True" in f.read_text(encoding="utf-8")


def test_apply_maintenance_plan_security_rejection(tmp_path: Path):
    """Changes targeting protected or escaping files are rejected."""
    plan = MaintenancePlan(
        audit_summary=AuditSummary(
            focus_area="Security",
            target_files=["config/api_keys.json", "../../escaped.py"],
            why_this_matters="Test security barrier",
        ),
        changes=[
            CodeChange(
                file_path="config/api_keys.json",
                original_snippet="{}",
                replacement_snippet='{"token": "leak"}',
                explanation="Malicious or erroneous modification of secret config",
            ),
            CodeChange(
                file_path="../../escaped.py",
                original_snippet="",
                replacement_snippet="malicious",
                explanation="Directory traversal attempt",
            ),
        ],
        pr_title="security test",
        pr_body="body",
        verification_notes=VerificationNotes(
            automated_checks=[],
            human_review_checklist=[],
        ),
    )

    success, modified, errors = apply_maintenance_plan(tmp_path, plan, dry_run=True)
    assert success is False
    assert len(errors) >= 2
    assert any("protected" in e.lower() for e in errors)
    assert any("escapes" in e.lower() for e in errors)
