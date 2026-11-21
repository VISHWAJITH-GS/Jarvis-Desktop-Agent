# Jarvis-Desktop-Agent — Repository Hygiene & Maintainability Audit

**Date**: 2026-11-21  
**Auditor**: Antigravity Agent  
**Baseline Test Status**: **302 passed**, 98 deselected (hardware dependent), 0 failed (100% passing test baseline).

---

## 1. Executive Summary

This audit assesses the code hygiene, structure, naming consistency, tracked artifacts, and maintainability across `Jarvis-Desktop-Agent` (Python 3.11/3.13, PyQt6, Gemini Live). It establishes the baseline metrics for Phases 1–5 of the repo hygiene refactoring roadmap.

---

## 2. Inventory & Metrics

### 2.1 File Size Audit (Files > 1,500 Lines)
The following files exceed the 1,500-line recommended ceiling:

| File | Line Count | Notes |
|---|---|---|
| `dashboard/server.py` | 1,741 | FastAPI/Starlette dashboard server and static handlers |
| `actions/dev_agent.py` | 1,694 | Self-improvement and coding agent engine |
| `actions/windows_uia.py` | 1,679 | Windows UI Automation driver and element inspector |

*Note*: Previous oversized monoliths (`ui.py` at ~13,300 lines and `main.py` at ~3,300 lines) have already been decomposed into `gui/` (each module < 1,320 lines) and `core/` (each module < 700 lines). `ui.py` is now 10 lines and `main.py` is 78 lines.

### 2.2 Top-Level Modules
Current root-level `.py` files:
1. `icons_manager.py`: GUI asset loader. Belongs in `gui/icons_manager.py`.
2. `verify_cdp_connection.py`: CDP connection test utility. Belongs in `scripts/verify_cdp_connection.py`.
3. `main.py`: Entry point (78 lines) — canonical.
4. `setup.py`: Build packaging script.
5. `ui.py`: Thin compatibility re-export facade (10 lines).

### 2.3 Duplicate & Stale Files
- Root duplicate copies of `action_executor.py`, `browser_session.py`, and `dom_distiller.py` were audited and merged into their canonical locations under `actions/`.
- Stale folder `copy UI/` (7 files) was verified redundant and deleted in commit `200e4bb`.
- `extensions/vimium_c/`: Third-party MIT-licensed Chrome extension code currently lives in `extensions/`. It should be moved to `third_party/vimium_c/` with appropriate licensing and upstream provenance documentation in `third_party/README.md`.

### 2.4 Tracked vs. Untracked Artifacts
Runtime artifacts generated during execution:
- `reports/` (untracked, ignored)
- `scratch/` (untracked, ignored)
- `memory/reminders/*.py` and `reminders_db.json` (untracked, ignored)
- `skills/skills_manifest.db` (untracked, ignored)
- `tests/fixtures/reminders_db.json` is preserved as a clean test fixture.

`.gitignore` covers cache, bytecode, and runtime artifacts, but needs standard coverage for `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `.coverage`, `htmlcov/`, `.idea/`, `.vscode/`, `*.docx`, `*.log`.

### 2.5 Naming Inconsistencies
- `setup.py`: declares `name="MARK LIV"`
- `LICENSE`: declares `MARK 56`
- `README.md`: mixed mentions of `Mark LVI` and `Jarvis-Desktop-Agent`
- **Resolution**: Unify around `Jarvis-Desktop-Agent`, version `1.0.0` defined in `core/version.py`, preserving attribution to `Mark LVI by FatihMakes` under CC BY-NC 4.0.

### 2.6 `print()` Calls in Non-Test Code
A total of **408** `print()` calls exist in non-test library code across packages:
- `actions/`: 140
- `core/`: 132
- Root modules (`icons_manager.py`, `setup.py`): 52
- `dashboard/`: 36
- `providers/`: 25
- `gui/`: 11
- `memory/`: 10
- `plugins/`: 2

These should be converted to standard `logging.getLogger(__name__)` calls in Phase 4.

### 2.7 Linting & Static Analysis (Ruff & Dead Code)
Running `ruff check --statistics .` identified:
- **986** unused imports (`F401`)
- **38** unused variables (`F841`)
- **18** redefined while unused (`F811`)
- **1,004** blind exceptions (`BLE001`)
- **226** unsorted imports (`I001`)
- *Vulture*: Not installed in local Python environment; dead code detection relies on Ruff `F401`, `F841`, and AST analysis.

---

## 3. Secret Scan & Personal Data Findings

### 3.1 Secret Scan
A full git history scan using regex patterns for Google API keys, OpenAI keys, Slack tokens, and GitHub personal access tokens identified:
- Exactly 1 match in `tests/test_secret_scan.py` (commit `f1bff2c`):
  `fake_key = "AIzaSyD-FakeTestKeyForJarvisSecretScan001"`
- **Result**: Confirmed fake test fixture. No active or live API keys or credentials exist in git history or working tree.

### 3.2 Personal Data in History
- Historical commits `b99befc` through `759ce95` committed personal reminders into `memory/reminders/reminders_db.json` (notes on exam preparation, TOC proofs, personal daily reminders).
- In commit `200e4bb`, the live file was removed from `memory/reminders/` and converted to `tests/fixtures/reminders_db.json`.

---

## 4. History Rewrite Plan (Optional / Gated)

> [!WARNING]
> Personal data exists in older commit history (`memory/reminders/reminders_db.json`).
> Rewrite prepared in audit doc; requires explicit user approval before execution.

If the user authorizes an immutable history cleanup, the following command using `git-filter-repo` will purge all historical revisions of `memory/reminders/reminders_db.json` without affecting fixtures:

```bash
# Prerequisites: pip install git-filter-repo
# Ensure clean working tree and local backup:
git clone . ../Jarvis-Desktop-Agent-backup

# Purge historical personal reminders file:
git filter-repo --path memory/reminders/reminders_db.json --invert-paths --force
```

*Status*: **Pending explicit user confirmation. Not executed.**

---

## 5. Roadmap

- [x] **Phase 0**: Baseline test suite verification and `docs/HYGIENE_AUDIT.md`.
- [ ] **Phase 1**: Gitignore hardening, verification of untracked artifacts, history plan documentation.
- [ ] **Phase 2**: Relocate `icons_manager.py` -> `gui/`, `verify_cdp_connection.py` -> `scripts/`, `extensions/vimium_c` -> `third_party/vimium_c`, unify version and naming.
- [ ] **Phase 3**: Configuration tooling (`pyproject.toml`, `.editorconfig`, `.gitattributes`, `.pre-commit-config.yaml`, mechanical `ruff format`, `requirements.lock` synchronization).
- [ ] **Phase 4**: Code health (mechanical removal of unused imports and replacement of library `print()` with standard logging).
- [ ] **Phase 5**: Documentation, `CONTRIBUTING.md`, `SECURITY.md`, `CHANGELOG.md`, `tests/test_config_template_sync.py`.
