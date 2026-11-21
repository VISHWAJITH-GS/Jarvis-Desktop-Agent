# Contributing to Jarvis-Desktop-Agent

Thank you for your interest in contributing to **Jarvis-Desktop-Agent**!  
Jarvis-Desktop-Agent is an open-source real-time desktop AI assistant (based on Mark LVI by FatihMakes), licensed under **[Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)** (Personal and Non-Commercial Use Only).

---

## 1. Core Rules for Contributors & Coding Agents

Before writing or modifying any code in this repository, you must read and adhere to [AGENTS.md](AGENTS.md):

1. **Threading Boundaries**:
   - `main.py` runs the voice session on an `asyncio` event loop in a background worker thread.
   - `ui.py` / `gui/` runs the graphical window on the **Qt main thread**.
   - Tools (`actions/*.py` and `plugins/*.py`) run on **worker threads**, never on the Qt main thread.
   - **Never create or touch a Qt widget from `actions/` or `plugins/`**. To display information, call thread-safe methods on `player` (e.g., `player.write_log()`, `player.show_content()`, `player.show_quiz()`, `player.show_review()`).
2. **Single QApplication**: Exactly one `QApplication` instance exists, created by `gui/window.py` via `ui.py`.
3. **AI Reasoning Through `core.gemini`**: All model calls must route through `core.gemini` (e.g., `gemini.text()`, `gemini.as_json()`), which handles rate limits, fallbacks, and user provider selection.
4. **Non-Commercial License**: All contributions must remain under the CC BY-NC 4.0 license.

---

## 2. Development Setup

### Prerequisites
- Python 3.11, 3.12, or 3.13
- Git

### Initializing Environment
```bash
# Clone the repository
git clone https://github.com/vishwajith-gs/Jarvis-Desktop-Agent.git
cd Jarvis-Desktop-Agent

# Create and activate virtual environment
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser engines (for browser automation)
python -m playwright install chromium firefox
```

---

## 3. Code Standards & Tooling

We enforce clean, formatted, and strictly typed Python code using modern tooling configured in `pyproject.toml`:

- **Formatter & Linter**: [Ruff](https://astral.sh/ruff)
  ```bash
  # Check lint issues
  ruff check .

  # Format codebase
  ruff format --exclude third_party .
  ```
- **Type Checking**:
  ```bash
  mypy core actions
  ```
- **Pre-commit**: We recommend installing pre-commit hooks:
  ```bash
  pre-commit install
  ```

---

## 4. Running Tests

Jarvis-Desktop-Agent features an automated test suite covering core orchestration, permissions, tasks, routines, and GUI facades.

```bash
# Run all unit and integration tests (excluding hardware-dependent tests)
pytest -m "not requires_hardware"

# Run a specific test suite
pytest tests/test_tasks_engine.py
pytest tests/test_routines.py
pytest tests/test_permissions_fail_closed.py
```

All pull requests must pass `pytest -m "not requires_hardware"` with zero test failures.

---

## 5. Commit & Pull Request Guidelines

- Use **Conventional Commits**:
  - `feat:` New features or capabilities
  - `fix:` Bug fixes
  - `refactor:` Code restructuring without functional alterations
  - `docs:` Documentation improvements
  - `style:` Formatting and stylistic changes
  - `test:` Adding or refining tests
  - `chore:` Dependency or repo maintenance
- Keep pull requests focused: one logical feature or bug fix per pull request.
