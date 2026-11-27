"""
scripts/daily_maintenance.py — Automated Daily Maintenance & Code Hardening for JARVIS.

Uses Gemini (with Live & REST fallback ladder or Google GenAI SDK) to perform targeted,
high-value, low-risk improvements across Mark-LVI-main without breaking existing behavior.

Features:
1. Dynamic Context Injection:
   - Pulls recent git commits (git log -n 5 --oneline) and recent changes (git diff HEAD~1).
   - Dynamically rotates through codebase files using LRU (least recently modified) or random sampling.
   - Employs core.code_checks.Protection to prevent reading or touching secrets / keys.
2. Structured Output Enforcement:
   - Uses Pydantic schemas (MaintenancePlan, CodeChange, AuditSummary, VerificationNotes)
     for programmatic patch application without regex parsing failures.
3. Patch Engine & Pre-flight Verification:
   - Precise snippet replacement with whitespace/line-ending normalization.
   - AST syntax validation prior to saving and automatic rollback on verification failure.
4. Git Branch & PR Automation:
   - Can automatically spin up a dedicated maintenance branch and format a pull request.

Usage:
    # Dry-run inspection (inspect files, generate plan, preview diffs without writing):
    python scripts/daily_maintenance.py --dry-run

    # Target specific files:
    python scripts/daily_maintenance.py --files actions/browser_control.py actions/whatsapp_tools.py --dry-run

    # Apply changes to disk and run automated checks:
    python scripts/daily_maintenance.py --apply

    # Apply, create git branch, commit, and prepare PR:
    python scripts/daily_maintenance.py --apply --branch --create-pr
"""

from __future__ import annotations

import argparse
import ast
import datetime
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pydantic import BaseModel, Field

# Core project utilities and safety guards
try:
    from core import gemini
except ImportError:
    gemini = None  # type: ignore

try:
    from core.code_checks import Protection
except ImportError:
    class Protection:  # type: ignore
        def __init__(self, folder: Path):
            self.folder = folder
        def reason(self, rel: str) -> str:
            low = rel.lower()
            if any(s in low for s in ("api_keys.json", ".env", "id_rsa", "credentials")):
                return "Protected security file"
            return ""

# Directories and files to always skip during context collection
_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "venv",
    ".venv",
    "env",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
    "scratch",
    "reports",
}

# ─────────────────────────────────────────────────────────────────────────────
# 1. System Prompt Specification
# ─────────────────────────────────────────────────────────────────────────────

MAINTENANCE_SYSTEM_PROMPT = """# ROLE
You are a Senior Python Systems & Desktop Automation Engineer maintaining the **JARVIS (`Mark-LVI-main`)** repository—a local Windows 11 voice and desktop automation assistant powered by Gemini Live API (`gemini-3.1-flash-live-preview`), Chrome DevTools Protocol (CDP) via Microsoft Edge, WhatsApp/Spotify automation, game update tracking, and a custom HTML/CSS/JS HUD.

# OBJECTIVE
Analyze the provided repository files and recent git diff, identify **one to three high-impact, low-risk improvements** for today's maintenance cycle, and output clean, verified code changes suitable for an automated Pull Request.

# IMPROVEMENT PRIORITIES (IN ORDER)
1. **Async & Live API Resilience**:
   - Add missing `try/except` blocks, timeouts, and exponential backoff retries around network calls, WebSocket streams, and Gemini API requests.
   - Fix potential race conditions, unclosed resources, or blocking I/O inside `asyncio` event loops.
2. **Browser & Desktop Automation Hardening**:
   - Harden Chrome DevTools Protocol (CDP), WhatsApp Web, and Spotify automation selectors by adding explicit wait conditions, fallback locators, and graceful failure handling when Edge or a target tab is closed.
   - Replace hardcoded Windows paths (e.g., `C:\\Users\\...`, `D:\\...`) with configurable `pathlib.Path` or environment variables.
3. **Security & Subprocess Hygiene**:
   - Flag or sanitize unsafe `subprocess` / PowerShell invocations triggered by voice commands.
   - Ensure no API keys, session tokens, or personal data are hardcoded.
4. **Code Quality, Typing & Tests**:
   - Add Python 3.13 type annotations and concise docstrings to untyped functions.
   - Remove dead code, unused imports, and leftover debug prints.
   - Add or update isolated `pytest` unit tests (mocking Windows/CDP dependencies) for core parsers, routers, and utilities.
5. **Documentation Sync**:
   - Update `README.md` or `SETUP.md` only if recent commits changed CLI flags, dependencies, or environment variables.

# STRICT GUARDRAILS (DO NOT VIOLATE)
- **Preserve Core Behavior**: Never alter wake-word logic, voice persona prompts, HUD visual styling/layout, or user-facing command triggers unless fixing a clear bug.
- **Minimal Diffs**: Modify at most 1–3 files per run. Do not rewrite entire working modules when a surgical patch solves the issue.
- **Cross-Platform CI Safety**: Ensure any new imports or tests work cleanly on a headless Linux GitHub Actions runner (mock `pywin32`, audio hardware, and Edge CDP in tests).
- **No Speculative Dependencies**: Do not introduce new third-party packages unless strictly necessary and added to the appropriate requirements file.

# REQUIRED OUTPUT FORMAT
Return your response strictly in the following structured format:

## 1. Audit Summary
- **Focus Area**: [Category from Priorities]
- **Target Files**: [List of files modified]
- **Why This Matters**: [1–2 sentences explaining the bug prevented or reliability gained]

## 2. Proposed Changes
For each modified or created file, provide the exact file path followed by the complete updated code block (or unified diff) with inline comments explaining non-obvious fixes:

### File: `path/to/file.py`
```python
# Complete updated code or clean patch
```

## 3. Verification & PR Notes
- **Automated Checks**: [Commands to verify syntax/tests, e.g., `python -m py_compile ...` or `pytest`]
- **Human Review Checklist**: [1–2 specific things the repository owner should double-check before merging the PR]
"""

# ─────────────────────────────────────────────────────────────────────────────
# 2. Pydantic Models for Structured Output Enforcement
# ─────────────────────────────────────────────────────────────────────────────

class CodeChange(BaseModel):
    file_path: str = Field(
        description="Relative file path from repo root (using forward slashes, e.g. 'actions/browser_control.py')."
    )
    original_snippet: str = Field(
        default="",
        description=(
            "Exact original snippet in the target file to replace. "
            "Leave as empty string if this is a newly created file."
        ),
    )
    replacement_snippet: str = Field(
        description=(
            "Replacement code snippet to swap in place of original_snippet. "
            "If original_snippet is empty, this is the full content of the new file."
        )
    )
    explanation: str = Field(
        default="",
        description="Brief comment explaining why this fix/hardening was made."
    )


class AuditSummary(BaseModel):
    focus_area: str = Field(
        description="Category from Priorities (e.g. 'Async & Live API Resilience' or 'Browser & Desktop Automation Hardening')."
    )
    target_files: list[str] = Field(
        description="List of relative file paths modified in this run (1 to 3 files max)."
    )
    why_this_matters: str = Field(
        description="1-2 sentences explaining the concrete bug prevented or reliability gained."
    )


class VerificationNotes(BaseModel):
    automated_checks: list[str] = Field(
        description="List of automated check commands (e.g. ['python -m py_compile ...', 'pytest tests/test_...'])."
    )
    human_review_checklist: list[str] = Field(
        description="1-2 specific items the repository owner should double-check before merging."
    )


class MaintenancePlan(BaseModel):
    audit_summary: AuditSummary
    changes: list[CodeChange] = Field(
        default_factory=list,
        description="List of 1 to 3 surgical code changes addressing today's maintenance focus.",
    )
    pr_title: str = Field(
        default="chore(maintenance): daily code hardening and improvements",
        description="Conventional commit pull request title (e.g. 'fix(cdp): add timeout and fallback locator to browser actions').",
    )
    pr_body: str = Field(
        default="",
        description="Complete markdown description for the Pull Request.",
    )
    verification_notes: VerificationNotes = Field(
        default_factory=lambda: VerificationNotes(automated_checks=[], human_review_checklist=[]),
        description="Verification notes and human review checklist items.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Dynamic Context Injection
# ─────────────────────────────────────────────────────────────────────────────

def get_git_history(repo_root: Path, n: int = 5) -> str:
    """Fetch the latest n commits on current branch."""
    try:
        res = subprocess.run(
            ["git", "log", f"-n{n}", "--oneline"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        out = res.stdout.strip()
        return out if out else "(No git history found or not a git repository)"
    except Exception as exc:
        return f"(Could not read git history: {exc})"


def get_git_diff(repo_root: Path) -> str:
    """Fetch recent git diff (HEAD~1 or uncommitted changes)."""
    try:
        # Try diff against HEAD~1
        res = subprocess.run(
            ["git", "diff", "HEAD~1", "--stat"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        stat = res.stdout.strip()
        if stat:
            # Also get a compact diff summary
            diff_res = subprocess.run(
                ["git", "diff", "HEAD~1", "-U2"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            compact_diff = diff_res.stdout[:8000]
            return f"DIFF STAT:\n{stat}\n\nRECENT DIFF SAMPLE:\n{compact_diff}"
        # Fallback to working directory diff
        status_res = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return status_res.stdout.strip() or "(Working tree clean, no recent diff)"
    except Exception as exc:
        return f"(Could not read git diff: {exc})"


def extract_outline(path: Path) -> str:
    """Extract class, method, and function signatures with line numbers."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        tree = ast.parse(text)
        outlines = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                line_no = getattr(node, "lineno", 1)
                line_text = lines[line_no - 1].strip() if line_no <= len(lines) else ""
                outlines.append(f"  Line {line_no:>4}: {line_text}")
        outlines.sort(key=lambda s: int(re.search(r"Line\s+(\d+)", s).group(1)))  # type: ignore
        return "\n".join(outlines[:60])
    except Exception:
        return "(Outline unavailable)"


def discover_python_files(repo_root: Path) -> list[Path]:
    """Discover candidate Python files in the repository, excluding tests/venv/protected."""
    protection = Protection(repo_root)
    candidates: list[Path] = []

    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if not f.endswith(".py"):
                continue
            full_path = Path(root) / f
            try:
                rel = full_path.relative_to(repo_root).as_posix()
            except ValueError:
                continue

            # Skip tests from being rotated as target for modifications unless explicitly targeted
            if rel.startswith("tests/") or rel.startswith("scripts/"):
                continue

            # Respect protection rules (keys, secrets, sensitive local configurations)
            if protection.reason(rel):
                continue

            candidates.append(full_path)

    return candidates


def select_rotating_files(
    repo_root: Path,
    candidates: list[Path],
    sample_size: int = 5,
    strategy: str = "lru",
    explicit_targets: list[str] | None = None,
) -> list[Path]:
    """Select a rotating subset of files using LRU, random, or explicit targets."""
    if explicit_targets:
        targets: list[Path] = []
        for t in explicit_targets:
            p = (repo_root / t).resolve()
            if p.is_file():
                targets.append(p)
            elif p.is_dir():
                targets.extend(list(p.glob("**/*.py")))
        return targets[:sample_size * 2]

    if not candidates:
        return []

    if strategy == "random":
        return random.sample(candidates, min(sample_size, len(candidates)))

    # Default "lru" (least-recently-modified): sort by st_mtime ascending
    # This ensures older, neglected parts of the codebase get daily attention
    sorted_by_mtime = sorted(candidates, key=lambda p: p.stat().st_mtime)

    # Balance selection: pick some from core/, actions/, plugins/
    buckets: dict[str, list[Path]] = {"core": [], "actions": [], "plugins": [], "other": []}
    for p in sorted_by_mtime:
        rel = p.relative_to(repo_root).as_posix()
        if rel.startswith("core/"):
            buckets["core"].append(p)
        elif rel.startswith("actions/"):
            buckets["actions"].append(p)
        elif rel.startswith("plugins/"):
            buckets["plugins"].append(p)
        else:
            buckets["other"].append(p)

    selected: list[Path] = []
    # Take 1-2 from each bucket until sample_size is satisfied
    for category in ("core", "actions", "plugins", "other"):
        if buckets[category] and len(selected) < sample_size:
            selected.append(buckets[category].pop(0))

    # Fill remaining from global oldest
    for p in sorted_by_mtime:
        if len(selected) >= sample_size:
            break
        if p not in selected:
            selected.append(p)

    return selected


def build_maintenance_context(
    repo_root: Path,
    files: list[Path],
    git_history: str,
    git_diff: str,
    char_budget: int = 80_000,
) -> str:
    """Format file contents, git context, and outlines within the character budget."""
    sections = [
        "### RECENT GIT COMMITS (HEAD~5):",
        git_history,
        "",
        "### RECENT GIT DIFF / REPO STATUS:",
        git_diff,
        "",
        "### ROTATING CANDIDATE SOURCE FILES FOR INSPECTION TODAY:",
    ]

    used_chars = sum(len(s) for s in sections)
    budget_per_file = max(4000, (char_budget - used_chars) // max(1, len(files)))

    for f in files:
        rel = f.relative_to(repo_root).as_posix()
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            sections.append(f"\n--- File: {rel} (Read Error: {exc}) ---")
            continue

        if len(content) <= budget_per_file:
            sections.append(f"\n--- File: {rel} (Full Content) ---\n{content}\n")
            used_chars += len(content)
        else:
            outline = extract_outline(f)
            head_snippet = "\n".join(content.splitlines()[:60])
            tail_snippet = "\n".join(content.splitlines()[-40:])
            summary = (
                f"\n--- File: {rel} (Partial Snippet & Outline - Total {len(content)} chars) ---\n"
                f"```python\n{head_snippet}\n# ... [content truncated for budget] ...\n{tail_snippet}\n```\n"
                f"Outlined Definitions:\n{outline}\n"
            )
            sections.append(summary)
            used_chars += len(summary)

    return "\n".join(sections)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Structured Generation with Gemini & Pydantic
# ─────────────────────────────────────────────────────────────────────────────

def call_gemini_for_maintenance(
    system_prompt: str,
    user_context: str,
    model_tier: str = "smart",
    timeout_ms: int = 120_000,
) -> MaintenancePlan:
    """Request structured MaintenancePlan using Gemini SDK or core.gemini ladder."""
    json_schema_example = """{
  "audit_summary": {
    "focus_area": "Async & Live API Resilience",
    "target_files": ["actions/browser_control.py"],
    "why_this_matters": "1-2 sentences on concrete reliability gained or bug prevented."
  },
  "changes": [
    {
      "file_path": "actions/browser_control.py",
      "original_snippet": "exact snippet from file to replace",
      "replacement_snippet": "new code to replace original snippet with",
      "explanation": "concise rationale"
    }
  ],
  "pr_title": "fix(browser): add timeout to cdp action execution",
  "pr_body": "Detailed PR explanation...",
  "verification_notes": {
    "automated_checks": ["python -m py_compile actions/browser_control.py"],
    "human_review_checklist": ["Verify CDP socket connects cleanly."]
  }
}"""

    prompt = (
        f"{user_context}\n\n"
        "INSTRUCTIONS:\n"
        "Identify 1 to 3 targeted, high-value, low-risk improvements following the priorities.\n"
        "Provide exact `original_snippet` strings from the files so our automated script can "
        "apply surgical replacements cleanly.\n\n"
        f"You MUST return valid JSON adhering strictly to this structure:\n{json_schema_example}\n"
    )

    # 1. Try google-genai SDK direct structured object generation
    client = None
    if gemini is not None:
        try:
            client = gemini.client(timeout_ms=timeout_ms)
        except Exception:
            client = None

    if client is None:
        env_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if env_key:
            try:
                from google import genai
                from google.genai import types as gtypes
                client = genai.Client(
                    api_key=env_key,
                    http_options=gtypes.HttpOptions(timeout=max(10_000, int(timeout_ms))),
                )
            except Exception:
                client = None

    if client is not None:
        from google.genai import types as gtypes
        sdk_models = ("gemini-2.5-flash", "gemini-3.1-pro-preview", "gemini-flash-latest")
        for target_model in sdk_models:
            try:
                config = gtypes.GenerateContentConfig(
                    system_instruction=system_prompt,
                    response_mime_type="application/json",
                    response_schema=MaintenancePlan,
                    temperature=0.2,
                )
                response = client.models.generate_content(
                    model=target_model,
                    contents=prompt,
                    config=config,
                )
                if response and response.text:
                    data = json.loads(response.text)
                    return MaintenancePlan.model_validate(data)
            except Exception as exc:
                print(f"[Daily Maintenance] Direct SDK call with model '{target_model}' failed ({exc})")

        print("[Daily Maintenance] Falling back to core.gemini ladder...")

    # 2. Fallback to core.gemini ladder (as_json)
    if gemini is not None:
        combined_prompt = (
            f"{system_prompt}\n\n{prompt}\n\n"
            "Return strictly the JSON object matching the schema. No markdown fences, no extra text."
        )
        data = gemini.as_json(
            combined_prompt,
            tier=model_tier,
            timeout_ms=timeout_ms,
            config={"response_mime_type": "application/json", "response_schema": MaintenancePlan},
        )
        if isinstance(data, dict):
            try:
                return MaintenancePlan.model_validate(data)
            except Exception as val_err:
                print(f"[Daily Maintenance] Schema validation error on returned JSON: {val_err}")

    raise RuntimeError(
        "Failed to generate structured maintenance plan from Gemini. "
        "Check your GEMINI_API_KEY and network connection."
    )

    raise RuntimeError(
        "Failed to generate structured maintenance plan from Gemini. "
        "Check your GEMINI_API_KEY and network connection."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. Patch Engine & Pre-flight Verification
# ─────────────────────────────────────────────────────────────────────────────

def normalize_text(text: str) -> str:
    """Normalize line endings and strip trailing line whitespace."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines)


def apply_snippet_replacement(file_content: str, original_snippet: str, replacement_snippet: str) -> tuple[str, bool, str]:
    """Perform surgical code replacement with normalization fallback."""
    if not original_snippet.strip():
        # New file creation or full overwrite
        return replacement_snippet, True, "Created / replaced entire file"

    # 1. Direct exact match
    if original_snippet in file_content:
        new_content = file_content.replace(original_snippet, replacement_snippet, 1)
        return new_content, True, "Exact match replacement successful"

    # 2. Line-ending normalized match
    norm_content = file_content.replace("\r\n", "\n")
    norm_original = original_snippet.replace("\r\n", "\n")
    norm_replacement = replacement_snippet.replace("\r\n", "\n")

    if norm_original in norm_content:
        new_norm_content = norm_content.replace(norm_original, norm_replacement, 1)
        # Restore CRLF if file originally had it
        if "\r\n" in file_content:
            new_norm_content = new_norm_content.replace("\n", "\r\n")
        return new_norm_content, True, "Line-ending normalized match successful"

    # 3. Stripped line-by-line fuzzy search
    norm_lines = [line.strip() for line in norm_original.strip().splitlines() if line.strip()]
    content_lines = norm_content.splitlines()

    for i in range(len(content_lines) - len(norm_lines) + 1):
        window = [content_lines[i + j].strip() for j in range(len(norm_lines))]
        if window == norm_lines:
            # Found matching window; determine indentation
            leading_indent = len(content_lines[i]) - len(content_lines[i].lstrip())
            indent_str = content_lines[i][:leading_indent]
            indented_replacement = "\n".join(
                (indent_str + line) if line.strip() else line
                for line in norm_replacement.splitlines()
            )
            before = content_lines[:i]
            after = content_lines[i + len(norm_lines):]
            new_norm_content = "\n".join(before + [indented_replacement] + after)
            if "\r\n" in file_content:
                new_norm_content = new_norm_content.replace("\n", "\r\n")
            return new_norm_content, True, "Whitespace-tolerant block match successful"

    return file_content, False, "Original snippet could not be located in target file"


def verify_syntax(path: Path, content: str) -> tuple[bool, str]:
    """Verify syntax using ast.parse for Python files."""
    if path.suffix == ".py":
        try:
            ast.parse(content, filename=str(path))
            return True, "AST syntax check passed"
        except SyntaxError as syn_err:
            return False, f"SyntaxError: {syn_err.msg} at line {syn_err.lineno}"
    elif path.suffix == ".json":
        try:
            json.loads(content)
            return True, "JSON syntax check passed"
        except Exception as exc:
            return False, f"JSON Error: {exc}"
    return True, "Check skipped for non-Python/non-JSON file"


def preview_diff(original: str, modified: str, file_path: str) -> str:
    """Generate unified diff string."""
    orig_lines = original.splitlines(keepends=True)
    mod_lines = modified.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines,
        mod_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=3,
    )
    return "".join(diff)


def apply_maintenance_plan(
    repo_root: Path,
    plan: MaintenancePlan,
    dry_run: bool = True,
) -> tuple[bool, list[str], list[str]]:
    """Apply the proposed changes to disk with safety checks and diff previews."""
    protection = Protection(repo_root)
    applied_files: list[str] = []
    errors: list[str] = []
    backups: dict[Path, str] = {}

    print(f"\n{'='*70}\nAPPLYING MAINTENANCE PLAN (Dry-run: {dry_run})\n{'='*70}")
    print(f"Focus Area: {plan.audit_summary.focus_area}")
    print(f"Why this matters: {plan.audit_summary.why_this_matters}\n")

    for idx, change in enumerate(plan.changes, 1):
        rel_path = change.file_path.strip().replace("\\", "/").lstrip("/")
        full_path = (repo_root / rel_path).resolve()

        # Security check: ensure path does not escape repo
        try:
            full_path.relative_to(repo_root)
        except ValueError:
            errors.append(f"Security error: path escapes repository root: {rel_path}")
            continue

        # Security check: ensure path is not protected
        if protection.reason(rel_path):
            errors.append(f"Security error: file is protected and cannot be modified: {rel_path}")
            continue

        print(f"[{idx}/{len(plan.changes)}] Evaluating change on: {rel_path}")
        if change.explanation:
            print(f"     Reason: {change.explanation}")

        original_content = ""
        if full_path.exists():
            original_content = full_path.read_text(encoding="utf-8", errors="replace")

        new_content, success, match_msg = apply_snippet_replacement(
            original_content, change.original_snippet, change.replacement_snippet
        )

        if not success:
            errors.append(f"Patch failed on {rel_path}: {match_msg}")
            print(f"     ❌ {match_msg}")
            continue

        print(f"     ✓ {match_msg}")

        # Syntax check
        valid, syn_msg = verify_syntax(full_path, new_content)
        if not valid:
            errors.append(f"Syntax validation failed on {rel_path}: {syn_msg}")
            print(f"     ❌ {syn_msg}")
            continue

        print(f"     ✓ {syn_msg}")

        # Show diff
        diff_text = preview_diff(original_content, new_content, rel_path)
        if diff_text:
            print(f"\n--- Unified Diff ({rel_path}) ---\n{diff_text}")
        else:
            print("     (No textual differences produced)")

        if not dry_run:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            backups[full_path] = original_content
            try:
                full_path.write_text(new_content, encoding="utf-8")
                applied_files.append(rel_path)
                print(f"     💾 Successfully written to disk: {rel_path}")
            except Exception as write_err:
                errors.append(f"Write error on {rel_path}: {write_err}")

    # If write errors occurred on a real run, rollback any modified files
    if not dry_run and errors and backups:
        print("\n⚠️ Errors occurred during write! Rolling back all changes...")
        for b_path, b_content in backups.items():
            if b_content:
                b_path.write_text(b_content, encoding="utf-8")
            elif b_path.exists():
                b_path.unlink()
        print("Rollback complete.")
        return False, applied_files, errors

    success_overall = len(errors) == 0 and len(plan.changes) > 0
    return success_overall, applied_files, errors


# ─────────────────────────────────────────────────────────────────────────────
# 6. Post-Change Verification & Testing
# ─────────────────────────────────────────────────────────────────────────────

def run_automated_checks(repo_root: Path, modified_files: list[str]) -> tuple[bool, list[str]]:
    """Run py_compile, ruff, and affected unit tests."""
    results: list[str] = []
    all_passed = True

    # 1. Compile check
    print("\n--- Running python -m py_compile ---")
    for f in modified_files:
        if f.endswith(".py"):
            target = repo_root / f
            res = subprocess.run(
                [sys.executable, "-m", "py_compile", str(target)],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                results.append(f"py_compile {f}: PASSED")
            else:
                all_passed = False
                err_msg = res.stderr.strip() or res.stdout.strip()
                results.append(f"py_compile {f}: FAILED -> {err_msg}")

    # 2. Ruff check (E9, F63, F7, F82 as per CI)
    print("--- Running ruff check ---")
    try:
        ruff_res = subprocess.run(
            ["ruff", "check", *modified_files, "--select=E9,F63,F7,F82"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if ruff_res.returncode == 0:
            results.append("ruff check: PASSED")
        else:
            all_passed = False
            results.append(f"ruff check: FAILED ->\n{ruff_res.stdout.strip()}")
    except FileNotFoundError:
        results.append("ruff check: SKIPPED (ruff not found)")

    # 3. Targeted pytest if test files relate to modified modules
    print("--- Running targeted pytest ---")
    candidate_tests = []
    for f in modified_files:
        stem = Path(f).stem
        # Check if tests/test_{stem}.py exists
        t1 = repo_root / "tests" / f"test_{stem}.py"
        if t1.exists():
            candidate_tests.append(str(t1.relative_to(repo_root)))

    if candidate_tests:
        pytest_res = subprocess.run(
            [sys.executable, "-m", "pytest", *candidate_tests, "-q"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if pytest_res.returncode == 0:
            results.append(f"pytest ({', '.join(candidate_tests)}): PASSED")
        else:
            all_passed = False
            results.append(f"pytest ({', '.join(candidate_tests)}): FAILED ->\n{pytest_res.stdout.strip()}")
    else:
        results.append("pytest: No directly matching test files found for modified modules.")

    return all_passed, results


# ─────────────────────────────────────────────────────────────────────────────
# 7. Git & PR Automation
# ─────────────────────────────────────────────────────────────────────────────

def create_git_branch_and_commit(
    repo_root: Path,
    plan: MaintenancePlan,
    modified_files: list[str],
    branch_name: str | None = None,
) -> str | None:
    """Create dedicated maintenance branch and commit changes."""
    if not modified_files:
        print("No files modified; skipping git commit.")
        return None

    today_str = datetime.datetime.now().strftime("%Y-%m-%d-%H%M")
    branch = branch_name or f"maintenance/daily-{today_str}"

    print(f"\n--- Git Automation: Branch '{branch}' ---")
    try:
        # Create and switch to new branch
        subprocess.run(["git", "checkout", "-b", branch], cwd=str(repo_root), check=True)
        # Stage modified files
        subprocess.run(["git", "add", *modified_files], cwd=str(repo_root), check=True)
        # Commit with PR title & body
        commit_msg = f"{plan.pr_title}\n\n{plan.pr_body}"
        subprocess.run(["git", "commit", "-m", commit_msg], cwd=str(repo_root), check=True)
        print(f"✓ Committed changes to branch '{branch}'")
        return branch
    except Exception as exc:
        print(f"Git operation failed: {exc}")
        return None


def create_github_pr(repo_root: Path, plan: MaintenancePlan, branch: str) -> bool:
    """Create GitHub Pull Request via gh CLI if installed."""
    if not shutil.which("gh"):
        print("GitHub CLI (`gh`) not installed. Please push the branch and open PR via GitHub web UI.")
        return False

    print("\n--- Submitting Pull Request via GitHub CLI ---")
    try:
        # Push branch
        subprocess.run(["git", "push", "-u", "origin", branch], cwd=str(repo_root), check=True)
        # Create PR
        pr_res = subprocess.run(
            ["gh", "pr", "create", "--base", "main", "--head", branch, "--title", plan.pr_title, "--body", plan.pr_body],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=True,
        )
        print(f"✓ Pull Request created: {pr_res.stdout.strip()}")
        return True
    except Exception as exc:
        print(f"Could not automatically create PR: {exc}")
        return False


def format_plan_as_markdown(plan: MaintenancePlan) -> str:
    """Render plan according to the prompt's Required Output Format."""
    md = []
    md.append("## 1. Audit Summary")
    md.append(f"- **Focus Area**: {plan.audit_summary.focus_area}")
    md.append(f"- **Target Files**: {', '.join(plan.audit_summary.target_files)}")
    md.append(f"- **Why This Matters**: {plan.audit_summary.why_this_matters}\n")

    md.append("## 2. Proposed Changes")
    for change in plan.changes:
        md.append(f"### File: `{change.file_path}`")
        if change.explanation:
            md.append(f"> {change.explanation}\n")
        md.append("```python")
        md.append(change.replacement_snippet.rstrip())
        md.append("```\n")

    md.append("## 3. Verification & PR Notes")
    md.append(f"- **Automated Checks**: {', '.join(plan.verification_notes.automated_checks)}")
    md.append("- **Human Review Checklist**:")
    for item in plan.verification_notes.human_review_checklist:
        md.append(f"  - {item}")

    md.append(f"\n### Suggested PR Title:\n`{plan.pr_title}`\n")
    md.append(f"### Suggested PR Body:\n{plan.pr_body}\n")

    return "\n".join(md)


# ─────────────────────────────────────────────────────────────────────────────
# 8. Main CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily Maintenance & Hardening Automation for JARVIS (Mark-LVI-main)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Inspect files, call Gemini, and show proposed patches without writing to disk (default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply proposed patches to disk after syntax verification.",
    )
    parser.add_argument(
        "--branch",
        action="store_true",
        help="Create a git branch and commit the applied changes.",
    )
    parser.add_argument(
        "--create-pr",
        action="store_true",
        help="Push branch and create a GitHub Pull Request via `gh` CLI.",
    )
    parser.add_argument(
        "--files",
        nargs="+",
        help="Specify one or more explicit files to inspect instead of random rotation.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=5,
        help="Number of rotating files to inspect (default: 5).",
    )
    parser.add_argument(
        "--strategy",
        choices=["lru", "random"],
        default="lru",
        help="File rotation strategy: 'lru' (least recently modified) or 'random'.",
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Path to write the generated markdown maintenance plan/report.",
    )
    parser.add_argument(
        "--budget",
        type=int,
        default=80_000,
        help="Context character budget for Gemini input (default: 80,000).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Gemini request timeout in seconds (default: 120).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    is_dry_run = not args.apply

    print("=" * 70)
    print("  JARVIS Daily Maintenance & Autonomous Hardening")
    print(f"  Mode: {'DRY RUN (Preview Only)' if is_dry_run else 'APPLY TO DISK'}")
    print("=" * 70)

    # 1. Gather dynamic git context
    print("\n[1/5] Collecting dynamic Git context...")
    git_history = get_git_history(REPO_ROOT, n=5)
    git_diff = get_git_diff(REPO_ROOT)
    print(f"✓ Git History: {len(git_history.splitlines())} commit(s) fetched")
    print(f"✓ Git Diff summary fetched ({len(git_diff)} chars)")

    # 2. Select rotating files
    print("\n[2/5] Selecting rotating candidate files for maintenance...")
    all_candidates = discover_python_files(REPO_ROOT)
    selected_files = select_rotating_files(
        repo_root=REPO_ROOT,
        candidates=all_candidates,
        sample_size=args.sample_size,
        strategy=args.strategy,
        explicit_targets=args.files,
    )

    if not selected_files:
        print("❌ No eligible source files found for inspection.")
        return 1

    print(f"✓ Selected {len(selected_files)} file(s) for inspection:")
    for f in selected_files:
        print(f"    - {f.relative_to(REPO_ROOT).as_posix()}")

    # 3. Build dynamic context
    print("\n[3/5] Building maintenance context payload...")
    context_text = build_maintenance_context(
        repo_root=REPO_ROOT,
        files=selected_files,
        git_history=git_history,
        git_diff=git_diff,
        char_budget=args.budget,
    )
    print(f"✓ Context compiled: {len(context_text)} characters (~{len(context_text) // 4} tokens)")

    # 4. Generate structured maintenance plan via Gemini
    print("\n[4/5] Consulting Gemini for high-impact, low-risk improvements...")
    try:
        plan = call_gemini_for_maintenance(
            system_prompt=MAINTENANCE_SYSTEM_PROMPT,
            user_context=context_text,
            timeout_ms=args.timeout * 1000,
        )
    except Exception as exc:
        print(f"❌ Failed to obtain maintenance plan: {exc}")
        return 1

    markdown_report = format_plan_as_markdown(plan)
    print("\n" + markdown_report)

    if args.output:
        out_p = Path(args.output).resolve()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(markdown_report, encoding="utf-8")
        print(f"✓ Report saved to {out_p}")

    # 5. Apply changes & verification
    print("\n[5/5] Patch execution and verification...")
    success, modified_files, errors = apply_maintenance_plan(
        repo_root=REPO_ROOT,
        plan=plan,
        dry_run=is_dry_run,
    )

    if not is_dry_run and modified_files:
        print("\nRunning automated verification on modified files...")
        ver_ok, ver_details = run_automated_checks(REPO_ROOT, modified_files)
        for detail in ver_details:
            print(f"  {detail}")

        if not ver_ok:
            print("\n❌ Automated checks failed! Manual review required.")
            return 1

        if args.branch:
            branch = create_git_branch_and_commit(REPO_ROOT, plan, modified_files)
            if branch and args.create_pr:
                create_github_pr(REPO_ROOT, plan, branch)

    if errors:
        print(f"\n⚠️ Encountered {len(errors)} error(s) during patch processing.")
        return 1

    print("\n✓ Daily maintenance cycle completed successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
