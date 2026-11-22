# Antigravity IDE Discovery Report

## 1. Installed Launchers & Version Analysis

### A. IDE Launcher (`antigravity-ide`)
- **Location on Windows**:
  - `C:\Users\VISHWAJITH\AppData\Local\Programs\Antigravity IDE\bin\antigravity-ide.cmd`
  - `C:\Users\VISHWAJITH\AppData\Local\Programs\Antigravity IDE\bin\antigravity-ide`
  - Main Executable: `C:\Users\VISHWAJITH\AppData\Local\Programs\Antigravity IDE\Antigravity IDE.exe`
- **Version**:
  - Version: `1.107.0`
  - Commit: `71bedb45923debaf49871bdcb7c7d3a47918e247`
  - Architecture: `x64`
- **Confirmed Command-Line Flags** (from `--help`):
  - `-d, --diff <file> <file>`: Compare two files with each other.
  - `-a, --add <folder>`: Add folder(s) to the last active window.
  - `--remove <folder>`: Remove folder(s) from the last active window.
  - `-g, --goto <file:line[:character]>`: Open a file at the path on the specified line and character position.
  - `-n, --new-window`: Force to open a new window.
  - `-r, --reuse-window`: Force to open a file or folder in an already opened window.
  - `-w, --wait`: Wait for the files to be closed before returning.
  - `--user-data-dir <dir>`: Specifies the directory that user data is kept in.
  - `--list-extensions`: List the installed extensions.
  - `-s, --status`: Print process usage and diagnostics information.
  - `chat [prompt]`: Open a chat with a prompt.

### B. Antigravity CLI Launcher (`agy`)
- **Location**:
  - `C:\Users\VISHWAJITH\.gemini\bin\agy.exe`
- **Version**:
  - Version: `1.5.14`
- **Headless / Non-Interactive Mode**:
  - **CONFIRMED**: Yes, full non-interactive mode is supported.
  - Flags:
    - `--print, -p, --prompt <string>`: Run a single prompt non-interactively and print the response.
    - `--output-format <text|json|stream-json>`: Structured output formatting.
    - `--input-format <text|stream-json>`: Structured input formatting.
    - `--dangerously-skip-permissions`: Headless execution without permission prompts.
  - Subcommands: `acp` (Agent Client Protocol stdio server), `models`, `agents`, `mcp`, `plugin`.

---

## 2. Remote Debugging & CDP Attachment Verification

### A. Dedicated Port & User Data Directory Test
- Executable: `Antigravity IDE.exe`
- Dedicated port: `9333` (isolated from Chrome/Edge CDP on port 9222)
- Dedicated profile directory: `%TEMP%\agy_cdp_test`
- Bound to: `127.0.0.1`
- Launched with:
  `--remote-debugging-port=9333 --user-data-dir=<profile_dir>`
- Environment variable guard: Must ensure `ELECTRON_RUN_AS_NODE` is removed from child process environment when invoking the Electron binary directly.

### B. HTTP `/json/version` Check
The IDE successfully responded at `http://127.0.0.1:9333/json/version`:
```json
{
  "Browser": "Chrome/142.0.7444.175",
  "Protocol-Version": "1.3",
  "User-Agent": "Mozilla/5.0 ... Electron/39.2.3 ...",
  "V8-Version": "14.2.231.21-electron.0",
  "WebKit-Version": "537.36 ...",
  "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/d7a7b82f-8703-49d6-8ee4-2a67e416a4f2"
}
```

### C. Target Discovery & Agent Panel Architecture
Inspected CDP targets via Playwright:
1. **Workbench Page**:
   - URL: `vscode-file://vscode-app/c:/Users/VISHWAJITH/AppData/Local/Programs/Antigravity%20IDE/resources/app/out/vs/code/electron-sandbox/workbench/workbench.html`
2. **Webview Wrapper Frames**:
   - URL: `vscode-webview://...`
3. **Antigravity Agent Panel Frame**:
   - URL: `http://127.0.0.1:<random_port>/?extensionView=true&extensionVariant=vs-code&hostVariant=...`
   - Hosted in an iframe within the secondary sidebar (`workbench.parts.auxiliarybar`).

### D. CDP vs UIA Decision
- **Evaluation**:
  - Windows UIA (`pywinauto.Desktop(backend="uia")`) returned 0 top-level windows in non-interactive sessions due to desktop station isolation.
  - In contrast, CDP on `http://127.0.0.1:9333` connects reliably via TCP WebSocket across session boundaries and provides direct access to both the outer workbench DOM and the inner Agent Panel iframe DOM.
- **Decision**: Authoritative driver is **CDP via a dedicated `AntigravitySessionManager`** (isolated from browser CDP), with launcher argument lists for CLI actions (`open_folder`, `open_file`, `open_diff`).

---

## 3. Agent Panel DOM & Selectors Mapping

All selectors are isolated in `actions/antigravity_selectors.py`:

| Component | Target / Role | Selector / Mechanism |
|---|---|---|
| Secondary Sidebar (Agent Panel Container) | Workbench Auxiliary Bar | `#workbench\.parts\.auxiliarybar`, `[aria-label="Antigravity, Antigravity"]` |
| Toggle / Show Agent Panel | Auxiliary Bar Toggle Button | `button[aria-label*="Antigravity"]`, `button[aria-label="Antigravity Section"]`, `[aria-label*="Secondary Side Bar"]` |
| New Conversation | Button | `button[aria-label*="New Conversation"]`, `button[aria-label*="New Chat"]` |
| Model Selector Dropdown | Combobox / Menu Button | `[data-testid="model-selector"]`, `[aria-label*="Select Model"]`, `button[role="combobox"]` |
| Prompt Input Composer | Textarea / ContentEditable | `textarea[placeholder*="Ask"]`, `textarea[aria-label*="Prompt"]`, `div[contenteditable="true"]` |
| Send Prompt Button | Button | `button[aria-label="Send"]`, `button[data-testid*="send"]` |
| Stop Agent Button | Button | `button[aria-label="Stop"]`, `button[data-testid*="stop"]` |
| Review: Accept All | Button | `button[aria-label*="Accept all"]`, `button:has-text("Accept all")` |
| Review: Reject All | Button | `button[aria-label*="Reject all"]`, `button:has-text("Reject all")` |
| Review: Per-File Buttons | Button | `button[aria-label*="Accept"]`, `button[aria-label*="Reject"]` |
| Terminal Command Prompts | Dialog / Action Bar | `button:has-text("Allow")`, `button:has-text("Run")`, `button:has-text("Approve")` *(NEVER auto-click!)* |
