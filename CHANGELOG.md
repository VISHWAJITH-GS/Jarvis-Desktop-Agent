# Changelog

All notable changes to **Jarvis-Desktop-Agent** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

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
