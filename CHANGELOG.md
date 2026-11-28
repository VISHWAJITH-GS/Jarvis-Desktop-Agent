# Changelog

All notable changes to **Jarvis-Desktop-Agent** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] - Phase 2: Files, Clipboard History, Notes, and Conversation Search

### Added
- **Clipboard Manager (`actions/clipboard_manager.py`)**:
  - Action-parameterized tool supporting `capture`, `paste`, `history`, `search`, `pin`, `pinned`, and `clear`.
  - SQLite WAL persistent storage in `memory/clipboard.db`.
  - Automated secret detection and masking using regex heuristics (`[REDACTED_SECRET]`) for JWTs, API tokens, passwords, and private keys.
  - Reversible action support integrated with `core.undo.UndoStack`.
- **Notes & Scratchpad Manager (`plugins/notes_manager.py`)**:
  - Full CRUD and tagging support for user notes and fast ephemeral scratchpads in `memory/notes.db`.
  - Daily note management (`daily_get`, `daily_append`) with markdown headers and timestamps.
- **Conversation Search Engine (`plugins/conversation_search.py`)**:
  - SQLite FTS5 full-text indexing and search for past conversation transcripts in `memory/conversations.db`.
  - Resilient query fallback with parameterized substring matching if FTS syntax errors occur.
- **Testing**:
  - Unit tests in `tests/test_phase2_tools.py` verifying secret masking, clipboard lifecycle, notes CRUD, and conversation FTS search.

---

## [1.2.0] - 2026-11-28 - Phase 1: Action Inspector, Audit Store & Metadata Routing

### Added
- **Action Inspector Tool (`actions/action_inspector.py`)**:
  - Unified inspection tool consolidating reference capabilities `action_history`, `action_detail`, `action_summary`, `dry_run`, and `tool_inspect`.
  - Full dry-run preview simulation evaluating argument validity, risk tier, confirmation requirement, and autonomous execution readiness without executing handlers.
  - Comprehensive catalog introspection listing tools, brief descriptions, and permission levels.
- **Persistent SQLite Audit Log (`core/permissions.py`)**:
  - SQLite WAL mode journal configuration for audit database (`audit.db`).
  - Added query methods: `query_audit_log` (with action/status/limit filters), `get_audit_entry_by_id`, and `get_audit_summary`.
- **ActionResult Extensions (`core/outcome.py`)**:
  - Added `ActionResult.unsupported` classmethod for clean machine-readable rejection of invalid actions.
- **Unit Testing**:
  - Dedicated test suite `tests/test_action_inspector.py` covering history, detail, summary, dry-run simulations, and error boundaries.

---

## [1.1.0] - 2026-11-23

### Added
- **Permission Settings System (`core/permissions.py`, `gui/settings_overlays.py`, `memory/config_manager.py`)**:
  - Centralized single registry (`PermissionRegistry`) of every system-altering and external action across 4 tiers (`READ_ONLY`, `NORMAL`, `SENSITIVE`, `DESTRUCTIVE`).
  - Configurable policy resolution (`ASK`, `ALLOW`, `DENY`) with optional timed allow mode ("allow for N minutes" with auto-reversion).
  - Tier defaults: `READ_ONLY` and `NORMAL` default to `ALLOW`; `SENSITIVE` and `DESTRUCTIVE` default to `ASK`.
  - Visible warning dialog when setting any `DESTRUCTIVE` permission to `ALLOW`.
  - Plugin auto-registration via `PLUGIN["permissions"]` list in tool definitions.
  - Model tamper-proofing: AI models and untrusted contexts are blocked from changing permission settings (`SecurityViolationError`).
  - "PERMISSIONS" drawer overlay in the Qt UI with live search, tier grouping, policy comboboxes, "Reset to defaults", and recent decisions audit log.
  - Persistent storage in `config/api_keys.json` with mtime cache.
- **Focus Mode (`plugins/modes.py`, `plugins/_focus_mode.py`, `memory/config_manager.py`)**:
  - System-level hosts file domain blocking inside `# === BEGIN JARVIS FOCUS MODE ===` markers with bundled adult domain list (`data/hosts_porn.txt`).
  - Optional secondary Family DNS filter (Cloudflare `1.1.1.3` / `1.0.0.3`) with original DNS restoration on exit or undo.
  - Secure DNS / DoH bypass detection and warning.
  - Background process monitor thread (poll every 2.5s) politely closing blocked apps (Instagram) via `WM_CLOSE` / `SIGTERM` (never kill).
  - Strict protection of always-allowed apps (VS Code, Antigravity, GitHub Desktop, Notion, Obsidian) and JARVIS's own PIDs.
  - MVP browser tab closer with pre-warning and verified title change check.
  - Crash safety via `data/focus_mode_state.json` restoring hosts and DNS on startup or process termination.
  - Graceful handling of missing administrative rights without silent failure.
- **English Placement Practice Mode (`plugins/modes.py`, `plugins/_english_mode.py`, `core/prompt_builder.py`)**:
  - Prompt overlay for campus placement coaching, forcing professional English responses mid-session with seamless reconnection while preserving context.
  - 6 structured interview scenarios: `self_intro`, `hr_round` (STAR methodology), `project_explain` (architecture & trade-offs), `code_walkthrough`, `group_discussion` (peer simulation), and `presentation` (2-minute talk).
  - Strict feedback engine: maximum 2 actionable corrections per answer with Original phrase, Polished rewrite, and One-line reason.
  - Real-time filler-word tracking ('um', 'like', 'basically', 'actually', 'you know') and average answer length metrics.
  - 3 feedback styles: `after_answer`, `end_of_session`, `on_request`.
  - Persistent mistake log in `memory/english_mistakes.json` with flashcard generation compatible with `player.show_quiz()` and spaced repetition.
  - Declared honest limits: grammar/vocabulary coaching is reliable; pronunciation and accent over STT is not; does not replace human mock interviews.


## [1.0.0] - 2026-11-21

### Added
- **Unified Background Task Engine (`core/tasks/`)**:
  - Persistent SQLite task store (`TaskStore`) for managing background execution states.
  - Fail-closed security validations, action permission tier verification, and step limits.
  - Resource and step budgets (`TaskBudget`) preventing runaway model loops.
  - Autonomous background engine (`TaskEngine`) with worker execution threads.
  - Natural language task actions (`actions/tasks.py`): `start_task`, `list_tasks`, `task_status`, `steer_task`, `stop_task`.
- **Consolidated Routines Engine (`core/routines/`)**:
  - Unified routine scheduler (`RoutineEngine`) supporting interval, cron, and daily scheduled routines.
  - Quiet Hours enforcement (`quiet_hours.py`) suppressing proactive alerts and routine speech during user-defined sleep/focus windows.
  - Automated database migration (`migration.py`) importing legacy reminders from JSON into persistent SQLite storage.
  - Routine voice and management tools (`actions/routines.py`): `create_routine`, `list_routines`, `edit_routine`, `delete_routine`, `configure_quiet_hours`.
- **Project Tooling & Configuration**:
  - Centralized `pyproject.toml` with Ruff, Mypy, and Pytest configuration.
  - Standardized `.editorconfig`, `.gitattributes` (LF normalization and binary flags), and `.pre-commit-config.yaml`.
  - Comprehensive documentation: `docs/ARCHITECTURE.md`, `docs/HYGIENE_AUDIT.md`, `CONTRIBUTING.md`, `SECURITY.md`.
  - Automated configuration template synchronization test (`tests/test_config_template_sync.py`).

### Changed
- **Architectural Modularization**:
  - Decomposed monolithic `ui.py` (~13,300 lines) into modern `gui/` package (`window.py`, `base.py`, `theme.py`, `hud.py`, `widgets.py`, `overlays.py`, `display_windows.py`, `docks.py`, `providers_panel.py`, `settings_overlays.py`, `telemetry_and_displays.py`, `window_controls.py`, `window_media.py`, `window_audio.py`, `window_panels.py`, `facade.py`). Kept `ui.py` as a thin 10-line backward-compatible re-export facade.
  - Decomposed monolithic `main.py` (~3,300 lines) into focused `core/` modules (`live_session.py`, `session_audio.py`, `session_monitors.py`, `session_voice.py`, `tool_dispatcher.py`, `prompt_builder.py`, `runtime_env.py`, `audio_endpoints.py`). Reduced `main.py` to a 78-line entry point.
  - Relocated root utility modules: `icons_manager.py` -> `gui/icons_manager.py`, `verify_cdp_connection.py` -> `scripts/verify_cdp_connection.py`.
  - Reorganized third-party Chrome extension assets: `extensions/vimium_c` -> `third_party/vimium_c` with provenance documentation.
- **Code Health & Hygiene**:
  - Pruned over 970 dead/unused imports across test and action modules.
  - Replaced direct library `print()` calls in actions and system identity modules with Python's standard `logging.getLogger(__name__)`.
  - Standardized code formatting across 200+ files using `ruff format`.

### Fixed
- Fixed `BASE_DIR` relative path resolution inside `gui/base.py` ensuring configuration and asset paths resolve to repo root rather than package directory.
- Fixed `NameError: name '_file_category' is not defined` in `gui/window_controls.py`.
- Resolved duplicate modules between root and `actions/` (`action_executor.py`, `browser_session.py`, `dom_distiller.py`).
- Corrected corrupted README reasoning table and updated project naming across `setup.py` and documentation.
