# Jarvis Desktop Agent — Comprehensive Tool Gap Analysis

Reference: `ankitjha67/JARVIS` (MIT Licensed, 231 tools in 65 modules).

## 1. Executive Summary & Inventory Overview

- **Total Reference Tools Evaluated**: 231
- **Status Exists**: 40 tools already available in Jarvis-Desktop-Agent
- **Status Partial**: 132 tools with existing foundations requiring completion/adapter
- **Status Missing**: 59 tools requiring new implementations

### Tool Consolidation & Payload Budget Strategy
Directly declaring 231 independent tools would consume over 180,000 characters in model system context, severely degrading latency, instruction following, and Gemini context economics.

Following **Rule 7**, all 231 capabilities are consolidated into **35–45 action-parameterized tools** using enumerated `action` parameters (e.g. `action='list'|'read'|'send'`), concise `description` fields, and compressed `brief` strings for tight budget trimming.

## 2. Capability Matrix & Gap Analysis

| Capability | Description | Status | Target File | Action/Plugin | Platform Support | Risk Tier | External Deps |
|---|---|---|---|---|---|---|---|
| `action_history` | Query the execution history of tool actions. Shows what ran, when, whether it succeeded or | **exists** | `actions/action_inspector.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `action_detail` | Get full details of a specific action from the audit log, including the arguments that wer | **exists** | `actions/action_inspector.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `action_summary` | Get a quick summary of recent action outcomes: how many succeeded, failed, were denied, or | **exists** | `actions/action_inspector.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `agent_create` | Create a background sub-agent task. | **partial** | `actions/agent_orchestrator.py` | `action` | Cross-platform | `NORMAL` | asyncio |
| `agent_list` | List background agent tasks. | **partial** | `actions/agent_orchestrator.py` | `action` | Cross-platform | `READ_ONLY` | asyncio |
| `agent_cancel` | Cancel a running agent task. | **partial** | `actions/agent_orchestrator.py` | `action` | Cross-platform | `NORMAL` | asyncio |
| `plan_create` | Create a multi-step plan for user review. | **partial** | `actions/agent_orchestrator.py` | `action` | Cross-platform | `READ_ONLY` | asyncio |
| `agent_status` | Get detailed status of a background agent task. | **partial** | `actions/agent_orchestrator.py` | `action` | Cross-platform | `READ_ONLY` | asyncio |
| `agent_handoff` | Delegate work to a background sub-agent. Can optionally wait for completion. | **partial** | `actions/agent_orchestrator.py` | `action` | Cross-platform | `NORMAL` | asyncio |
| `plan_action` | Approve, advance, or cancel a plan. | **partial** | `actions/agent_orchestrator.py` | `action` | Cross-platform | `NORMAL` | asyncio |
| `ambient_scan` | List all visible windows with their title, process name, PID and screen position. | **partial** | `actions/system_sentinel.py` | `action` | Windows/macOS | `READ_ONLY` | pywin32 / psutil |
| `ambient_watch` | Start or stop background monitoring of window focus changes. | **partial** | `actions/system_sentinel.py` | `action` | Windows/macOS | `NORMAL` | pywin32 / psutil |
| `ambient_focus_history` | Return recent window focus changes recorded by ambient_watch. | **partial** | `actions/system_sentinel.py` | `action` | Windows/macOS | `READ_ONLY` | pywin32 / psutil |
| `bg_task_create` | Create a persistent background task that runs asynchronously with checkpointing. | **partial** | `actions/tasks.py` | `action` | Cross-platform | `NORMAL` | sqlite3, threading |
| `bg_task_status` | Check the status and progress of a background task. | **partial** | `actions/tasks.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3, threading |
| `bg_task_list` | List background tasks, optionally filtered by status or type. | **partial** | `actions/tasks.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3, threading |
| `bg_task_cancel` | Cancel a queued or running background task. | **partial** | `actions/tasks.py` | `action` | Cross-platform | `NORMAL` | sqlite3, threading |
| `browser_open` | Open a web page in Jarvis's browser window. Follow with browser_snapshot to see it. | **partial** | `actions/browser_control.py` | `action` | Cross-platform | `NORMAL` | playwright |
| `browser_snapshot` | Read the current web page: its text (start) and its links, buttons and fields, each with a | **partial** | `actions/browser_control.py` | `action` | Cross-platform | `READ_ONLY` | playwright |
| `browser_read` | Return the full visible text of the current web page. | **partial** | `actions/browser_control.py` | `action` | Cross-platform | `READ_ONLY` | playwright |
| `browser_click` | Click a link, button or other element from the latest browser_snapshot. | **partial** | `actions/browser_control.py` | `action` | Cross-platform | `SENSITIVE` | playwright |
| `browser_type` | Fill a text field from the latest browser_snapshot. Refuses password, payment and code fie | **partial** | `actions/browser_control.py` | `action` | Cross-platform | `SENSITIVE` | playwright |
| `browser_press` | Press a key in the current web page (for example PageDown to scroll). | **partial** | `actions/browser_control.py` | `action` | Cross-platform | `NORMAL` | playwright |
| `browser_navigate` | Go back, forward, reload, or close the current tab. | **partial** | `actions/browser_control.py` | `action` | Cross-platform | `NORMAL` | playwright |
| `browser_tabs` | List the tabs open in Jarvis's browser window. | **partial** | `actions/browser_control.py` | `action` | Cross-platform | `READ_ONLY` | playwright |
| `buffer_profiles` | List connected Buffer social media profiles. | **missing** | `plugins/social_scheduler.py` | `plugin` | Cross-platform | `READ_ONLY` | requests |
| `buffer_post` | Create or schedule a social media post via Buffer. | **missing** | `plugins/social_scheduler.py` | `plugin` | Cross-platform | `SENSITIVE` | requests |
| `buffer_queue` | View pending posts in a Buffer profile's queue. | **missing** | `plugins/social_scheduler.py` | `plugin` | Cross-platform | `READ_ONLY` | requests |
| `buffer_analytics` | Get analytics for a Buffer profile's recent posts. | **missing** | `plugins/social_scheduler.py` | `plugin` | Cross-platform | `READ_ONLY` | requests |
| `case_save` | Create a local case file or replace its fields using the current revision. Stores evidence | **partial** | `actions/tasks.py` | `action` | Cross-platform | `SENSITIVE` | sqlite3 |
| `case_get` | Read a case file including its current revision, evidence and draft. | **partial** | `actions/tasks.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `case_list` | List local case files ordered by deadline, with next actions and status. | **partial** | `actions/tasks.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `chrome_list_tabs` | List open Chrome browser tabs. | **partial** | `actions/browser_session.py` | `action` | Cross-platform | `READ_ONLY` | websockets, requests |
| `chrome_navigate` | Navigate the active Chrome tab to a URL. | **partial** | `actions/browser_session.py` | `action` | Cross-platform | `NORMAL` | websockets, requests |
| `chrome_get_content` | Get the text content of the current Chrome page. | **partial** | `actions/browser_session.py` | `action` | Cross-platform | `READ_ONLY` | websockets, requests |
| `chrome_screenshot` | Take a screenshot of the current Chrome tab. | **partial** | `actions/browser_session.py` | `action` | Cross-platform | `READ_ONLY` | websockets, requests |
| `chrome_run_js` | Execute JavaScript in the current Chrome tab. | **partial** | `actions/browser_session.py` | `action` | Cross-platform | `SENSITIVE` | websockets, requests |
| `chrome_click` | Click an element in Chrome by CSS selector. | **partial** | `actions/browser_session.py` | `action` | Cross-platform | `NORMAL` | websockets, requests |
| `chrome_type` | Type text into a form field in Chrome. | **partial** | `actions/browser_session.py` | `action` | Cross-platform | `NORMAL` | websockets, requests |
| `clipboard_capture` | Snapshot the current clipboard text into the clipboard history. | **exists** | `actions/clipboard_manager.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | pyperclip, sqlite3 |
| `clipboard_history` | Browse or search the clipboard history. | **exists** | `actions/clipboard_manager.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | pyperclip, sqlite3 |
| `clipboard_pin` | Pin a text snippet for quick recall later. | **exists** | `actions/clipboard_manager.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | pyperclip, sqlite3 |
| `clipboard_pinned` | List all pinned clipboard snippets. | **exists** | `actions/clipboard_manager.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | pyperclip, sqlite3 |
| `clipboard_paste` | Copy a history entry back to the clipboard. | **exists** | `actions/clipboard_manager.py` | `action` | Windows/macOS/Linux | `NORMAL` | pyperclip, sqlite3 |
| `clipboard_search` | Search clipboard history or list recent entries. | **exists** | `actions/clipboard_manager.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `clipboard_get` | Get a specific clipboard entry by ID. | **exists** | `actions/clipboard_manager.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `codereview_diff` | Show git diff (working tree or staged) for a repository. | **partial** | `actions/dev_tools.py` | `action` | Cross-platform | `READ_ONLY` | git |
| `codereview_log` | Show recent git commit log. | **partial** | `actions/dev_tools.py` | `action` | Cross-platform | `READ_ONLY` | git |
| `codereview_blame` | Run git blame on a file, optionally for a line range. | **partial** | `actions/dev_tools.py` | `action` | Cross-platform | `READ_ONLY` | git |
| `codereview_stats` | Show file-level change statistics (git diff --stat). | **partial** | `actions/dev_tools.py` | `action` | Cross-platform | `READ_ONLY` | git |
| `email_list` | List recent emails (newest first), optionally filtered by words, sender or unread. | **missing** | `plugins/email_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | imaplib, smtplib |
| `email_read` | Read one email in full. | **missing** | `plugins/email_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | imaplib, smtplib |
| `email_send` | Send an email from the user's account. Show the user the exact text first when it is long. | **missing** | `plugins/email_manager.py` | `plugin` | Cross-platform | `SENSITIVE` | imaplib, smtplib |
| `call_start` |  | **partial** | `plugins/email_manager.py` | `plugin` | Cross-platform | `SENSITIVE` | imaplib, smtplib |
| `settings_open` | Open a page of {var} (then use ui_snapshot / ui_click to change something). | **partial** | `plugins/email_manager.py` | `plugin` | Cross-platform | `NORMAL` | imaplib, smtplib |
| `ui_snapshot` | List the controls (buttons, fields, menus, links…) of the window in the foreground, each w | **partial** | `actions/windows_uia.py` | `action` | Windows | `READ_ONLY` | uiautomation / ctypes |
| `ui_click` | Click a control from the latest ui_snapshot. | **partial** | `actions/windows_uia.py` | `action` | Windows | `SENSITIVE` | uiautomation / ctypes |
| `ui_type` | Type text into a control from ui_snapshot, or at the current cursor. Never use for passwor | **partial** | `actions/windows_uia.py` | `action` | Windows | `SENSITIVE` | uiautomation / ctypes |
| `ui_hotkey` | Press a key or keyboard shortcut in the active window. | **partial** | `actions/windows_uia.py` | `action` | Windows | `SENSITIVE` | uiautomation / ctypes |
| `conversation_search` | Search past conversations for a topic, decision, or phrase. | **exists** | `plugins/conversation_search.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `conversation_list` | List recent conversations by title and date. | **exists** | `plugins/conversation_search.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `credential_check_password` | Check password strength locally: entropy, common-pattern detection, suggestions. The passw | **partial** | `actions/security_privacy.py` | `action` | Cross-platform | `READ_ONLY` | hashlib, requests |
| `credential_check_reuse` | Check if a password appears in known data breaches via HIBP Pwned Passwords (k-anonymity:  | **partial** | `actions/security_privacy.py` | `action` | Cross-platform | `READ_ONLY` | hashlib, requests |
| `credential_generate` | Generate a cryptographically secure random password. | **partial** | `actions/security_privacy.py` | `action` | Cross-platform | `READ_ONLY` | hashlib, requests |
| `apps_search` | Find installed applications by name. | **exists** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, pyautogui |
| `apps_open` | Launch an installed application by name. | **exists** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `NORMAL` | psutil, pyautogui |
| `windows_list` | List open top-level windows with their ids, titles and owning processes. | **exists** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, pyautogui |
| `window_focus` | Bring a window to the front. | **exists** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `NORMAL` | psutil, pyautogui |
| `window_set_state` | Minimize, maximize or restore a window. | **exists** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `NORMAL` | psutil, pyautogui |
| `window_close` | Ask a window to close. The app may prompt to save; unsaved work can be lost. | **exists** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | psutil, pyautogui |
| `web_open` | Open a web page in the default browser. | **partial** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `NORMAL` | psutil, pyautogui |
| `clipboard_read` | Read the text currently on the clipboard. | **missing** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, pyautogui |
| `clipboard_write` | Replace the clipboard contents with text. | **missing** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `NORMAL` | psutil, pyautogui |
| `power_lock` | Lock the computer (shows the {var} lock screen). | **exists** | `actions/desktop_control.py` | `action` | Windows/macOS/Linux | `NORMAL` | psutil, pyautogui |
| `git_status` | Show git status of a repository. | **exists** | `actions/dev_tools.py` | `action` | Cross-platform | `READ_ONLY` | git, subprocess |
| `git_log` | Show recent git commits. | **exists** | `actions/dev_tools.py` | `action` | Cross-platform | `READ_ONLY` | git, subprocess |
| `git_diff` | Show git diff (working tree or staged). | **exists** | `actions/dev_tools.py` | `action` | Cross-platform | `READ_ONLY` | git, subprocess |
| `error_explain` | Explain an error message and suggest fixes. | **partial** | `actions/dev_tools.py` | `action` | Cross-platform | `READ_ONLY` | git, subprocess |
| `project_launch` | Launch a project (auto-detects package.json, Cargo.toml, etc). | **partial** | `actions/dev_tools.py` | `action` | Cross-platform | `NORMAL` | git, subprocess |
| `digest_now` | Build and deliver a morning-style digest right now. | **partial** | `plugins/digest_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `digest_preview` | Preview the digest without delivering it. | **partial** | `plugins/digest_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `dry_run` | Preview what a tool would do WITHOUT executing it. Shows the tool's tier, whether it needs | **exists** | `actions/action_inspector.py` | `action` | Cross-platform | `READ_ONLY` | core/confirm.py |
| `tool_inspect` | Inspect available tools: list all tools with their tiers, or get details about a specific  | **exists** | `actions/action_inspector.py` | `action` | Cross-platform | `READ_ONLY` | core/confirm.py |
| `files_mkdir` | Create a folder (and any missing parent folders). | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS/Linux | `NORMAL` | ctypes / os |
| `files_move` | Move, rename or copy a file or folder. Never overwrites. | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | ctypes / os |
| `files_delete` | Send a file or folder to the {var} (recoverable from there). | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | ctypes / os |
| `power_action` |  | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | ctypes / os |
| `file_extract` | Extract and return text content from PDF, DOCX, XLSX, CSV, PPTX, JSON, ZIP, or any text fi | **partial** | `actions/file_processor.py` | `action` | Cross-platform | `READ_ONLY` | pypdf, docx, openpyxl, pptx |
| `file_convert` | Convert a file to another format (text/docx→PDF, docx/pdf→TXT, xlsx→CSV). | **partial** | `actions/file_processor.py` | `action` | Cross-platform | `NORMAL` | pypdf, docx, openpyxl, pptx |
| `file_search` | Full-text search across indexed files. | **partial** | `actions/file_controller.py` | `action` | Cross-platform | `READ_ONLY` | os, pathlib |
| `file_name_search` | Search file names in indexed folders. | **partial** | `actions/file_controller.py` | `action` | Cross-platform | `READ_ONLY` | os, pathlib |
| `file_index_folder` | Index a folder for full-text search. | **partial** | `actions/file_controller.py` | `action` | Cross-platform | `NORMAL` | os, pathlib |
| `web_clip` | Save a web page bookmark with notes. | **partial** | `actions/file_controller.py` | `action` | Cross-platform | `READ_ONLY` | os, pathlib |
| `daily_note` | Append to today's daily note. | **partial** | `actions/file_controller.py` | `action` | Cross-platform | `READ_ONLY` | os, pathlib |
| `files_list` | List the contents of a folder. | **exists** | `actions/file_controller.py` | `action` | Cross-platform | `READ_ONLY` | os, shutil, send2trash |
| `files_search` | Find files by name under a folder, newest first. | **exists** | `actions/file_controller.py` | `action` | Cross-platform | `READ_ONLY` | os, shutil, send2trash |
| `files_read` | Read a text file (first 64 KB). | **exists** | `actions/file_controller.py` | `action` | Cross-platform | `READ_ONLY` | os, shutil, send2trash |
| `files_write` | Create a text file, or replace one when overwrite is true. | **exists** | `actions/file_controller.py` | `action` | Cross-platform | `SENSITIVE` | os, shutil, send2trash |
| `files_open` | Open a file or folder with its default application. | **partial** | `actions/file_controller.py` | `action` | Cross-platform | `NORMAL` | os, shutil, send2trash |
| `fitness_reminder` | Set a recurring health reminder (hydration, posture, eye break, stretch, etc.). | **partial** | `plugins/productivity.py` | `plugin` | Cross-platform | `NORMAL` | sqlite3 |
| `fitness_check` | Check all active health reminders and see which are currently due. | **partial** | `plugins/productivity.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `fitness_log` | Log a health/fitness activity (steps, water intake, exercise, etc.). | **partial** | `plugins/productivity.py` | `plugin` | Cross-platform | `NORMAL` | sqlite3 |
| `fitness_summary` | Summarise today's logged fitness/health activities with totals per type. | **partial** | `plugins/productivity.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `flight_search` | Search for flights between two cities. Returns Google Flights and Skyscanner links with tr | **missing** | `plugins/weather_travel.py` | `plugin` | Cross-platform | `READ_ONLY` | requests |
| `flight_open` | Open a flight search URL in the default browser. | **missing** | `plugins/weather_travel.py` | `plugin` | Cross-platform | `SENSITIVE` | requests |
| `focus_start` | Start a timed deep-work session. Tracks distractions via the ambient context radar. Does N | **partial** | `plugins/modes.py` | `plugin` | Windows/macOS/Linux | `SENSITIVE` | hosts file, psutil |
| `focus_status` | Check the current focus session: active, time remaining, distractions detected. | **partial** | `plugins/modes.py` | `plugin` | Windows/macOS/Linux | `READ_ONLY` | hosts file, psutil |
| `focus_end` | End the current focus session early and return a summary. | **partial** | `plugins/modes.py` | `plugin` | Windows/macOS/Linux | `NORMAL` | hosts file, psutil |
| `focus_stats` | Return history of recent focus sessions with duration and distraction counts. | **partial** | `plugins/modes.py` | `plugin` | Windows/macOS/Linux | `READ_ONLY` | hosts file, psutil |
| `gods_eye_setup` | Clone and install God's Eye View (3D geospatial intelligence globe). Downloads the repo an | **missing** | `plugins/mcp_geospatial.py` | `plugin` | Cross-platform | `SENSITIVE` | requests (Opt-in) |
| `gods_eye_start` | Start the God's Eye View dev server on port 4173. Required before using any God's Eye View | **missing** | `plugins/mcp_geospatial.py` | `plugin` | Cross-platform | `SENSITIVE` | requests (Opt-in) |
| `gods_eye_stop` | Stop the God's Eye View dev server. | **missing** | `plugins/mcp_geospatial.py` | `plugin` | Cross-platform | `NORMAL` | requests (Opt-in) |
| `gods_eye_status` | Check whether God's Eye View is installed and its dev server is running. | **missing** | `plugins/mcp_geospatial.py` | `plugin` | Cross-platform | `READ_ONLY` | requests (Opt-in) |
| `ha_list` | List Home Assistant devices by type. | **missing** | `plugins/home_assistant.py` | `plugin` | Cross-platform | `READ_ONLY` | requests / aiohttp |
| `ha_control` | Turn on, off, or toggle a Home Assistant device. | **missing** | `plugins/home_assistant.py` | `plugin` | Cross-platform | `SENSITIVE` | requests / aiohttp |
| `ha_scene` | Activate a Home Assistant scene. | **missing** | `plugins/home_assistant.py` | `plugin` | Cross-platform | `NORMAL` | requests / aiohttp |
| `ha_temperature` | Set thermostat temperature. | **missing** | `plugins/home_assistant.py` | `plugin` | Cross-platform | `SENSITIVE` | requests / aiohttp |
| `image_generate` | Generate an image from a text prompt using local Stable Diffusion (if running) or return s | **missing** | `plugins/image_generator.py` | `plugin` | Cross-platform | `SENSITIVE` | requests (A1111/ComfyUI API) |
| `image_list` | List recently generated images. | **missing** | `plugins/image_generator.py` | `plugin` | Cross-platform | `READ_ONLY` | requests (A1111/ComfyUI API) |
| `incident_capture` | Capture a system state snapshot: CPU, memory, disk, top processes, recent event log errors | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `NORMAL` | psutil, winreg |
| `incident_process_detail` | Get detailed info about a process by name or PID. | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, winreg |
| `incident_network` | Show active network connections with process info. | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, winreg |
| `incident_service_status` |  | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, winreg |
| `document_intake` | Extract configured Label: Value fields from local documents with page or line evidence and | **partial** | `actions/file_processor.py` | `action` | Cross-platform | `READ_ONLY` | pypdf, markdown |
| `mcp_catalogue` | Browse available MCP servers to install. | **missing** | `plugins/mcp_client.py` | `plugin` | Cross-platform | `READ_ONLY` | mcp sdk / subprocess |
| `mcp_install` | Enable an MCP server from the catalogue. | **missing** | `plugins/mcp_client.py` | `plugin` | Cross-platform | `SENSITIVE` | mcp sdk / subprocess |
| `mcp_uninstall` | Remove an MCP server from your configuration. | **missing** | `plugins/mcp_client.py` | `plugin` | Cross-platform | `SENSITIVE` | mcp sdk / subprocess |
| `volume_set` | Set, change or mute the system output volume. Returns the resulting level. | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS/Linux | `NORMAL` | pyautogui, pycaw |
| `media_control` | Send a media key: play_pause, next, previous or stop. | **partial** | `actions/computer_settings.py` | `action` | Windows/macOS/Linux | `NORMAL` | pyautogui, pycaw |
| `memory_remember` | Save a fact, preference, or instruction to long-term memory. | **partial** | `actions/memory_controller.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `memory_recall` | Search long-term memory for relevant facts. | **partial** | `actions/memory_controller.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `memory_forget` | Remove a specific memory by ID. | **partial** | `actions/memory_controller.py` | `action` | Cross-platform | `NORMAL` | sqlite3 |
| `memory_review` | Review pending auto-extracted memories. | **partial** | `actions/memory_controller.py` | `action` | Cross-platform | `READ_ONLY` | sqlite3 |
| `music_search` | Search for music across YouTube Music. Returns song/album/artist results. | **exists** | `actions/spotify_control.py` | `action` | Cross-platform | `READ_ONLY` | spotipy, lyricsgenius |
| `music_play` | Play a song: search by name or open a direct YouTube Music / Spotify URL. | **exists** | `actions/spotify_control.py` | `action` | Cross-platform | `SENSITIVE` | spotipy, lyricsgenius |
| `music_lyrics` | Fetch song lyrics by artist and title. | **partial** | `actions/spotify_control.py` | `action` | Cross-platform | `READ_ONLY` | spotipy, lyricsgenius |
| `note_add` | Save a quick note with optional tags. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `note_list` | List recent notes, optionally filtered by tag. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `note_search` | Search notes by text content. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `note_delete` | Delete a note by its ID. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `NORMAL` | sqlite3 |
| `note_today` | Get or create today's daily note. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `note_daily_append` | Append text to today's daily note under a section. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `NORMAL` | sqlite3 |
| `persona_get` | Get the current persona and standing instructions. | **partial** | `plugins/persona_presets.py` | `plugin` | Cross-platform | `READ_ONLY` | json |
| `persona_set` | Update the persona and standing instructions. | **partial** | `plugins/persona_presets.py` | `plugin` | Cross-platform | `SENSITIVE` | json |
| `persona_append` | Add a standing instruction to the persona. | **partial** | `plugins/persona_presets.py` | `plugin` | Cross-platform | `NORMAL` | json |
| `preset_list` | List available configuration presets. | **partial** | `plugins/persona_presets.py` | `plugin` | Cross-platform | `READ_ONLY` | json |
| `preset_apply` | Apply a configuration preset. | **partial** | `plugins/persona_presets.py` | `plugin` | Cross-platform | `NORMAL` | json |
| `privacy_net_audit` | Audit active network connections: see which processes are talking to the internet. | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, socket |
| `privacy_dns_lookup` | Resolve a hostname and log the result for the privacy audit trail. | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, socket |
| `privacy_listening_ports` | List all listening (open) ports and which processes own them. | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, socket |
| `privacy_startup_audit` |  | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, socket |
| `project_create` | Scaffold a new project: python, node, react, fastapi, flask, nextjs, or html. | **partial** | `actions/dev_tools.py` | `action` | Cross-platform | `SENSITIVE` | pathlib |
| `project_analyze` | Analyze a project directory: detect language, framework, file stats, dependencies and entr | **partial** | `actions/dev_tools.py` | `action` | Cross-platform | `READ_ONLY` | pathlib |
| `brightness_get` | Get current screen brightness level (0-100). | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS | `READ_ONLY` | pycaw, screen_brightness_control |
| `quick_brightness_set` | Set screen brightness (quick settings). | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS | `SENSITIVE` | pycaw, screen_brightness_control |
| `quick_volume_set` | Set system volume level (quick settings). | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS | `NORMAL` | pycaw, screen_brightness_control |
| `quick_wifi_toggle` | Enable or disable Wi-Fi (quick settings). | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS | `SENSITIVE` | pycaw, screen_brightness_control |
| `quick_bluetooth_toggle` | Enable or disable Bluetooth (quick settings). | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS | `SENSITIVE` | pycaw, screen_brightness_control |
| `night_light_toggle` | Toggle Windows Night Light. | **partial** | `actions/computer_settings.py` | `action` | Windows/macOS | `SENSITIVE` | pycaw, screen_brightness_control |
| `regulatory_compare` | Compare two local numbered regulation revisions, with page or line evidence and optional c | **partial** | `plugins/compliance_tools.py` | `plugin` | Cross-platform | `READ_ONLY` | difflib |
| `deep_research` | Run a multi-step deep research pipeline: decomposes the question into sub-questions, searc | **partial** | `actions/deep_research.py` | `action` | Cross-platform | `SENSITIVE` | requests, bs4 |
| `research_status` | Check the status of deep research tasks. Returns active/completed reports. | **partial** | `actions/deep_research.py` | `action` | Cross-platform | `READ_ONLY` | requests, bs4 |
| `rc_subscriber` | Get a RevenueCat subscriber's entitlements and purchase history. | **partial** | `plugins/mobile_companion.py` | `plugin` | Cross-platform | `READ_ONLY` | requests (Opt-in) |
| `rc_offerings` | List available RevenueCat offerings and packages. | **partial** | `plugins/mobile_companion.py` | `plugin` | Cross-platform | `READ_ONLY` | requests (Opt-in) |
| `rc_grant` | Grant a promotional entitlement to a subscriber. | **partial** | `plugins/mobile_companion.py` | `plugin` | Cross-platform | `SENSITIVE` | requests (Opt-in) |
| `rc_overview` | Get a RevenueCat app overview (subscriber count, MRR). | **partial** | `plugins/mobile_companion.py` | `plugin` | Cross-platform | `READ_ONLY` | requests (Opt-in) |
| `routine_create` | Create a scheduled routine: morning_briefing (daily summary), eod_recap (end-of-day wrap-u | **exists** | `actions/routines.py` | `action` | Cross-platform | `NORMAL` | json, core/undo.py |
| `routine_list` | List all active scheduled routines. | **exists** | `actions/routines.py` | `action` | Cross-platform | `READ_ONLY` | json, core/undo.py |
| `routine_delete` | Delete a scheduled routine by its task ID. | **exists** | `actions/routines.py` | `action` | Cross-platform | `NORMAL` | json, core/undo.py |
| `undo_last` | Undo the last reversible action (file write, move, copy, clipboard change). | **exists** | `actions/routines.py` | `action` | Cross-platform | `NORMAL` | json, core/undo.py |
| `undo_list` | List recent actions that can be undone. | **exists** | `actions/routines.py` | `action` | Cross-platform | `READ_ONLY` | json, core/undo.py |
| `schedule_create` | Create a timer, reminder, or recurring task. | **partial** | `actions/scheduler.py` | `action` | Cross-platform | `READ_ONLY` | threading, time |
| `schedule_list` | List scheduled tasks and reminders. | **partial** | `actions/scheduler.py` | `action` | Cross-platform | `READ_ONLY` | threading, time |
| `schedule_cancel` | Cancel a scheduled task. | **partial** | `actions/scheduler.py` | `action` | Cross-platform | `NORMAL` | threading, time |
| `scratchpad_add` | Add a quick note to the scratchpad. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `NORMAL` | sqlite3 |
| `scratchpad_list` | List scratchpad notes, newest first. Optionally filter by tag. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `scratchpad_search` | Full-text search across scratchpad notes. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | sqlite3 |
| `scratchpad_delete` | Delete a scratchpad note by ID. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `NORMAL` | sqlite3 |
| `scratchpad_clear` | Delete ALL scratchpad notes. This cannot be undone. | **exists** | `plugins/notes_manager.py` | `plugin` | Cross-platform | `SENSITIVE` | sqlite3 |
| `clip_capture` | Take a screenshot (full screen or active window) and save it with metadata. | **partial** | `actions/screenshot_curator.py` | `action` | Cross-platform | `NORMAL` | Pillow, sqlite3 |
| `clip_list` | List saved screenshots, optionally filtered by tag or label search. | **partial** | `actions/screenshot_curator.py` | `action` | Cross-platform | `READ_ONLY` | Pillow, sqlite3 |
| `clip_delete` | Delete a screenshot by ID. | **partial** | `actions/screenshot_curator.py` | `action` | Cross-platform | `NORMAL` | Pillow, sqlite3 |
| `clip_tag` | Add tags to a screenshot. | **partial** | `actions/screenshot_curator.py` | `action` | Cross-platform | `READ_ONLY` | Pillow, sqlite3 |
| `file_find` |  | **partial** | `actions/file_controller.py` | `action` | Cross-platform | `READ_ONLY` | os, sqlite3 |
| `content_search` | Search inside files for text content (like grep). | **partial** | `actions/file_controller.py` | `action` | Cross-platform | `READ_ONLY` | os, sqlite3 |
| `security_lock` | Lock the screen immediately. | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | psutil, ctypes |
| `security_processes` | List running processes (filtered by name). | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, ctypes |
| `security_network` | Show active network connections. | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil, ctypes |
| `security_kill` | Kill a process by PID. | **partial** | `actions/security_privacy.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | psutil, ctypes |
| `sentinel_create` | Create a named condition watcher. Supported: cpu_above, memory_above, disk_above, process_ | **partial** | `actions/system_sentinel.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | psutil |
| `sentinel_list` | List all active sentinel watchers and their last-checked status. | **partial** | `actions/system_sentinel.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil |
| `sentinel_check` | Run all (or one) sentinel watchers now and return which ones triggered. | **partial** | `actions/system_sentinel.py` | `action` | Windows/macOS/Linux | `NORMAL` | psutil |
| `sentinel_remove` | Remove a sentinel watcher by name. | **partial** | `actions/system_sentinel.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil |
| `brightness_set` | Set the display brightness (0-100). Works on laptops and some monitors. | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS | `NORMAL` | pycaw, netsh |
| `wifi_toggle` | Enable or disable the Wi-Fi adapter. | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS | `SENSITIVE` | pycaw, netsh |
| `bluetooth_toggle` | Enable or disable the Bluetooth radio. | **exists** | `actions/computer_settings.py` | `action` | Windows/macOS | `SENSITIVE` | pycaw, netsh |
| `open_settings` | Open a {var} settings page by name. | **partial** | `actions/computer_settings.py` | `action` | Windows/macOS | `NORMAL` | pycaw, netsh |
| `shell_run` | Run a {var} command as the current (non-administrator) user and return its output. Use onl | **missing** | `actions/sandboxed_shell.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | subprocess |
| `skill_list` | List available skills. | **partial** | `core/plugin_loader.py` | `core` | Cross-platform | `READ_ONLY` | pathlib |
| `skill_create` | Create a new skill from instructions or recorded macro. | **partial** | `core/plugin_loader.py` | `core` | Cross-platform | `NORMAL` | pathlib |
| `system_status` | Current CPU, memory, GPU, disk, battery and network usage of this computer. | **exists** | `actions/system_monitor.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil |
| `process_list` | List running processes, sorted by memory or CPU. | **exists** | `actions/system_monitor.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | psutil |
| `process_kill` | Terminate a process by pid. Unsaved work in it is lost. | **exists** | `actions/system_monitor.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | psutil |
| `telegram_send` | Send a message to the user via Telegram. | **missing** | `plugins/telegram_manager.py` | `plugin` | Cross-platform | `SENSITIVE` | requests / httpx |
| `telegram_status` | Check Telegram bot connection status. | **missing** | `plugins/telegram_manager.py` | `plugin` | Cross-platform | `READ_ONLY` | requests / httpx |
| `timer_set` | Set a countdown timer. Plays a chime when done. | **missing** | `actions/timers_stopwatch.py` | `action` | Cross-platform | `READ_ONLY` | threading, time |
| `timer_list` | List all active timers and their remaining time. | **missing** | `actions/timers_stopwatch.py` | `action` | Cross-platform | `READ_ONLY` | threading, time |
| `timer_cancel` | Cancel an active timer. | **missing** | `actions/timers_stopwatch.py` | `action` | Cross-platform | `READ_ONLY` | threading, time |
| `stopwatch` | Start, stop, lap, or reset a stopwatch. | **missing** | `actions/timers_stopwatch.py` | `action` | Cross-platform | `READ_ONLY` | threading, time |
| `reminder_set` | Set a reminder. Specify minutes from now or an absolute time. | **partial** | `actions/timers_stopwatch.py` | `action` | Cross-platform | `NORMAL` | threading, time |
| `reminder_list` | List upcoming reminders. | **partial** | `actions/timers_stopwatch.py` | `action` | Cross-platform | `READ_ONLY` | threading, time |
| `reminder_cancel` | Cancel a pending reminder. | **partial** | `actions/timers_stopwatch.py` | `action` | Cross-platform | `NORMAL` | threading, time |
| `uat_trace` | Build a local UAT coverage and regression review from requirement and test registers with  | **partial** | `actions/action_inspector.py` | `action` | Cross-platform | `READ_ONLY` | json |
| `verify_last_action` | Verify whether the most recent tool action succeeded by checking its outcome (file exists, | **partial** | `actions/verify_outcome.py` | `action` | Cross-platform | `READ_ONLY` | core/outcome.py |
| `model_stats` | Show model ladder usage statistics: calls, latency, success rate and escalations per tier. | **partial** | `actions/verify_outcome.py` | `action` | Cross-platform | `READ_ONLY` | core/outcome.py |
| `screen_capture` | Take a screenshot of the screen or a region. | **exists** | `actions/screen_vision.py` | `action` | Cross-platform | `READ_ONLY` | Pillow, pytesseract |
| `screen_ocr` | Run OCR on the latest screenshot and return text. | **partial** | `actions/screen_vision.py` | `action` | Cross-platform | `READ_ONLY` | Pillow, pytesseract |
| `screen_search` | Search through screen capture history (OCR text). | **partial** | `actions/screen_vision.py` | `action` | Cross-platform | `READ_ONLY` | Pillow, pytesseract |
| `voice_call` | Call the user's phone and speak a message using text-to-speech (Twilio). Use when Jarvis n | **missing** | `plugins/voice_call.py` | `plugin` | Cross-platform | `SENSITIVE` | twilio |
| `voice_call_status` | Check the status of a Twilio voice call (queued, ringing, in-progress, completed, failed,  | **missing** | `plugins/voice_call.py` | `plugin` | Cross-platform | `READ_ONLY` | twilio |
| `voice_call_config` | Check whether voice calling is configured (Twilio credentials and phone number). | **missing** | `plugins/voice_call.py` | `plugin` | Cross-platform | `READ_ONLY` | twilio |
| `weather_get` | Get current weather and 3-day forecast. Defaults to the user's current location if none sp | **partial** | `plugins/weather_travel.py` | `plugin` | Cross-platform | `READ_ONLY` | requests |
| `workflow_record_start` | Start recording a named workflow (macro). Subsequent workflow_record_step calls add steps. | **partial** | `actions/workflow_recorder.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | pyautogui, sqlite3 |
| `workflow_record_step` | Add a step to the workflow currently being recorded. | **partial** | `actions/workflow_recorder.py` | `action` | Windows/macOS/Linux | `NORMAL` | pyautogui, sqlite3 |
| `workflow_record_stop` | Stop recording and save the current workflow. | **partial** | `actions/workflow_recorder.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | pyautogui, sqlite3 |
| `workflow_list` | List all saved workflows with their step counts. | **partial** | `actions/workflow_recorder.py` | `action` | Windows/macOS/Linux | `READ_ONLY` | pyautogui, sqlite3 |
| `workflow_replay` | Return the steps of a saved workflow for the orchestrator to replay. | **partial** | `actions/workflow_recorder.py` | `action` | Windows/macOS/Linux | `SENSITIVE` | pyautogui, sqlite3 |
| `workflow_delete` | Delete a saved workflow. | **partial** | `actions/workflow_recorder.py` | `action` | Windows/macOS/Linux | `NORMAL` | pyautogui, sqlite3 |
| `youtube_search` | Search YouTube and return a list of matching videos. | **exists** | `actions/youtube_video.py` | `action` | Cross-platform | `READ_ONLY` | yt-dlp, youtube-transcript-api |
| `youtube_play` | Open a YouTube video in the default browser. | **exists** | `actions/youtube_video.py` | `action` | Cross-platform | `SENSITIVE` | yt-dlp, youtube-transcript-api |
| `youtube_transcript` | Extract the transcript (subtitles/auto-captions) from a YouTube video. | **partial** | `actions/youtube_video.py` | `action` | Cross-platform | `READ_ONLY` | yt-dlp, youtube-transcript-api |
