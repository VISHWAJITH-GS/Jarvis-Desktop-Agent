# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |

---

## Security Architecture & Design Principles

Jarvis-Desktop-Agent runs with direct operating system access, file manipulation capabilities, and external network interactions. To safeguard the host environment, the system implements defense-in-depth:

### 1. Permission Matrix & Fail-Closed Guardrails
Every tool is classified into explicit security tiers (`core/permissions.py`):
- **Tier 1 (Informational / Read-Only)**: Autonomous execution without interruption (e.g. `web_search`, `read_file`, `list_tasks`).
- **Tier 2 (Safe Interactive)**: Local workspace edits and bounded actions (e.g. `dev_agent`, `start_task`).
- **Tier 3 (Confirmation Required)**: Destructive actions, system setting changes, or process terminations require explicit user confirmation.
- **Tier 4 (Blocked / Dangerous)**: High-risk system calls that are completely disabled in standard mode.
- *Fail-Closed*: Any unregistered action or tool without an explicit permission entry is blocked by default.

### 2. Prompt Injection Defense
- Natural language instructions are strictly separated from system control commands.
- Untrusted web content retrieved by browsers or scraping tools is sanitized before being passed into model context.
- High-risk operations (such as code execution or shell actions) are gated behind verification loops.

### 3. Local Credential Storage
- API keys and tokens are stored in `config/api_keys.json` with restricted OS file permissions (`icacls` on Windows, `chmod 600` on POSIX), or in the OS Keyring where available.
- `config/api_keys.json` is strictly ignored by version control (`.gitignore`).
- Templates (`config/api_keys.template.json`) contain only placeholders and are verified by automated tests (`tests/test_config_template_sync.py`).

---

## Reporting a Vulnerability

If you discover a security vulnerability in Jarvis-Desktop-Agent, please do NOT file a public issue.

Instead, please report it via private disclosure:
- Email: **vishwajithgs006@gmail.com**
- Include detailed steps to reproduce, impact analysis, and proof-of-concept where possible.

We appreciate responsible disclosure and will respond within 48 hours to evaluate and patch identified security issues.
