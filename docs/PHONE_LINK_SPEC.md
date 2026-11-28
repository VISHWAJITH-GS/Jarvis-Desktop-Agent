# Microsoft Phone Link (`phone_link`) — Technical Specification & Phase Plan

## 1. Overview & Architecture
- **Module Placement:**
  - `actions/phone_link.py` — Single action-parameterized `TOOL` (`name="phone_link"`) auto-discovered by `core/action_loader.py`.
  - `actions/_phone_link_driver.py` — Shared UIA driver, mutex, window/clipboard save-restore, selector resolver, and capability detector (leading underscore prevents direct tool discovery).
  - `config/phone_link_selectors.json` — Locale-keyed UIA selector tables (`AutomationId` + `ControlType` + localized `Name`), user-extendable.
- **Platform Guard:** Windows-only. Lazy-imports `pywinauto` / `comtypes` via `core/lazyimport.py`. Non-Windows platforms return `ActionResult(status="unsupported", ...)`.
- **No Public API:** Controlled strictly via **Windows UI Automation (UIA)** and documented Phone Link keyboard shortcuts (`Ctrl+.`, `Ctrl+,`, `Ctrl+1..4`, `Ctrl+P`, `Ctrl+N`, `Shift+Enter`, `Ctrl+Alt+K`; Photos: `Ctrl+C`, `Ctrl+O`, `Ctrl+H`, `Delete`).
- **Runtime Discovery:** Process name (`PhoneExperienceHost.exe` observed), top-level window title/class, and `AutomationId`s are discovered at runtime and never hardcoded from guesses.

---

## 2. Capability Matrix (Android vs. iPhone)

| Feature / Capability | Android (`Link to Windows`, Android 10+) | iPhone (Win 11, Bluetooth, iOS 14/15+) | Notes & Hard Limits |
|---|---|---|---|
| **Status / Launch / Navigation** | Supported | Supported | Requires Phone Link installed & linked. |
| **Calls (Dial, Answer, Decline, End, Digits)** | Supported (BT + HFP) | Supported (BT + HFP) | Max 100 recent calls; **cannot delete** call history from PC. No BT-headset relay for iPhone calls. **Emergency numbers blocked.** |
| **Messages (SMS)** | Supported (last 30 days) | Partial (connected via BT only) | Android: RCS only on select Samsung + Samsung Messages; third-party apps (WhatsApp) do NOT sync. iPhone: **no group chats, no media**. **Cannot delete** messages from PC. |
| **Messages (MMS Attachment)** | Supported (requires Settings > Features > Messages toggle) | Unsupported | Must verify file path is in user-approved folders. |
| **Notifications (List, Dismiss, Clear All)** | Supported | Supported (while connected) | Clear all uses `Ctrl+Alt+K`. Dismissed notifications cannot be undone. |
| **Phone Controls (DND, Mute Phone)** | Supported (device-dependent) | Unsupported | Undoable via `core/undo.py` by restoring previous state. |
| **Photos (List, Copy, Save)** | Supported (last 2,000 images) | Unsupported | `share_photo` and `delete_photo` are **DISABLED by default**. |
| **Apps & Phone Screen (`Ctrl+P`)** | Select Android devices only | Unsupported | Capability probe detects availability per device/feature toggle. |
| **Experimental `dial_via_tel_uri`** | Optional (off by default) | Optional (off by default) | Disabled by default due to inconsistent `tel:` handler behavior. |

---

## 3. UIA Selector Strategy & Fragility Design
1. **No Blind Coordinates:** Never click guessed `(x, y)` positions.
2. **Resolution Order:**
   1. `AutomationId` (locale-independent primary selector)
   2. `ControlType` + localized `Name` from `config/phone_link_selectors.json` (checked against active Windows UI language, falling back through configured locales; logs matched locale without PII).
3. **Failure Behavior:** If a selector cannot be resolved within bounded retries/timeout, return `ActionResult(status="failed", ...)` naming the missing selector key and suggesting `action="diagnose"`.
4. **Concurrency & State Preservation:**
   - Single `threading.RLock()` UIA mutex ensures one Phone Link operation at a time.
   - Saves and restores the user's foreground window handle.
   - Saves and restores the clipboard **only** when an action modifies it (e.g., `copy_photo` intentionally leaves the photo on the clipboard; paste-based typing restores prior clipboard).
5. **Capability Probing:** On first use, probes linked OS (`android` vs `iphone` vs `unlinked`), enabled Phone Link `Settings > Features` toggles, and visible navigation tabs; caches briefly and invalidates on failure. Disabled features return `status="unsupported"`.

---

## 4. Action Inventory, Tiers & Verification Contract

| Action | Parameters | Risk Tier | Undoable | Verification / Feasibility |
|---|---|---|---|---|
| `status` | — | `READ_ONLY` | No | Reads window presence, linked device type, connection state, enabled features. |
| `launch` | — | `READ_ONLY` | No | Launches Phone Link (`ms-phone:` / Appx) and verifies top-level window appears. |
| `open_feature` | `feature` (`calls\|messages\|photos\|apps\|notifications\|settings`) | `READ_ONLY` | No | Navigates via `Ctrl+1..4` / `Ctrl+,` or nav tab and verifies active view header. |
| `diagnose` | — | `READ_ONLY` | No | Writes a **redacted** UIA tree dump to `docs/phone_link_uia_dumps/` (or user data dir) with PII scrubbed. |
| `recent_calls` | `limit` (max 100) | `READ_ONLY` | No | Scrapes call log list; wraps names/numbers in untrusted markers. |
| `call` | `target` (contact or number) | `SENSITIVE` | No | Refuses emergency numbers; disambiguates contacts; `core/confirm.py` gate; verifies active-call UI state. |
| `answer` | — | `NORMAL` | No | Clicks Answer button via UIA; verifies active-call state. |
| `decline` | — | `NORMAL` | No | Clicks Decline button via UIA; verifies incoming-call banner gone. |
| `end_call` | — | `NORMAL` | No | Clicks End Call button via UIA; verifies call UI exited. |
| `dial_digits` | `digits` | `NORMAL` | No | Only allowed during an active call; sends DTMF digits to active call dialpad. |
| `call_status` | — | `READ_ONLY` | No | Reads active/incoming/idle call state. |
| `list_conversations` | `limit` | `READ_ONLY` | No | Lists recent SMS threads; wraps contact names/snippets as untrusted; redacts OTPs in logs. |
| `read_conversation` | `contact`, `limit` | `READ_ONLY` | No | Opens thread and reads messages; wraps in untrusted markers; redacts OTPs in audit/logs. |
| `send_sms` | `target`, `text` | `SENSITIVE` | No | Disambiguates contact; `core/confirm.py` gate; sends via UIA/composer; re-reads thread to match outgoing bubble (else `unverified`). |
| `send_mms_attachment` | `target`, `file` | `SENSITIVE` | No | Android only + MMS feature toggle check; validates `file` inside approved folders; `core/confirm.py` gate; verifies outgoing message. |
| `list_notifications` | `limit` | `READ_ONLY` | No | Reads notification cards; wraps in untrusted markers. |
| `dismiss_notification` | `app` or `index` | `NORMAL` | No | Dismisses target card; verifies item count decreased / item gone. |
| `clear_all` | — | `NORMAL` | No | Sends `Ctrl+Alt+K`; verifies notification list empty. |
| `dnd` | `state` (`on\|off`) | `NORMAL` | **Yes** | Android/supported only; toggles DND, verifies new state, pushes `core/undo.py` restoration. |
| `mute_phone` | `state` (`on\|off`) | `NORMAL` | **Yes** | Android/supported only; toggles mute, verifies new state, pushes `core/undo.py` restoration. |
| `list_recent_photos` | `limit` | `READ_ONLY` | No | Android only; returns metadata only (index, timestamp/label). |
| `copy_photo` | `index` | `NORMAL` | No | Android only; selects photo and sends `Ctrl+C`; verifies clipboard image change. |
| `save_photo` | `index`, `folder` | `NORMAL` | No | Android only; validates `folder` is user-approved; triggers Save As / copy and verifies file exists. |
| `share_photo` | `index` | `SENSITIVE` | No | **DISABLED by default.** Confirmation-gated if enabled. |
| `delete_photo` | `index` | `DESTRUCTIVE` | No | **DISABLED by default.** Confirmation-gated if enabled; verifies item removed. |
| `list_apps` | — | `READ_ONLY` | No | Android supported devices only; lists synced mobile apps. |
| `open_app` | `name` | `NORMAL` | No | Android supported devices only; launches mobile app window. |
| `open_phone_screen` | — | `NORMAL` | No | Android supported devices only; sends `Ctrl+P` and verifies screen mirror window. |
| `dial_via_tel_uri` | `number` | `SENSITIVE` | No | **DISABLED by default** (experimental fallback). |

---

## 5. Non-Negotiable Safety & Privacy Rules
1. **UI-Issued Confirmation Gate (`core/confirm.py`):** `call`, `send_sms`, `send_mms_attachment`, `share_photo`, `delete_photo` require UI/user confirmation. Model can never self-confirm.
2. **Emergency Number Blocklist:** `112, 100, 101, 102, 108, 911, 999` (+ configurable list) are always refused with instruction to dial directly from the phone.
3. **Prompt Injection Defense:** All SMS, notifications, contact names, and call logs are wrapped via `core.outcome.untrusted(...)`. Reading any untrusted Phone Link content escalates the session run so any subsequent `SENSITIVE`/`DESTRUCTIVE` action requires fresh confirmation even if previously approved.
4. **Contact Disambiguation:** Multiple plausible fuzzy matches return `status="needs_confirmation"` listing candidates with masked phone numbers (last 4 digits only) — never guesses.
5. **OTP / Banking Privacy:** OTP-like codes (4–8 digit verification codes with OTP/bank/auth context) are redacted in logs, audit entries, and conversation history; read aloud only when explicitly requested for that message; never forwarded in outbound actions.
6. **Audit Privacy:** No plaintext message bodies or full phone numbers in logs/audit DB — store only action, contact alias / last-4 digits, outcome, and timestamp.
7. **Default-Disabled Feature Flags:** Group toggles (`calls`, `messages`, `notifications`, `photos`, `apps`, `watcher`, `share_photo`, `delete_photo`, `dial_via_tel_uri`) all default to `False` in `config/settings`, exposed with visible toggles in the UI.
8. **Rate Limiting:** Configurable per-minute cap on outbound calls and SMS/MMS sends.

---

## 6. Phased Implementation Plan (1 Commit per Phase)

- **Phase 1 — Driver, Discovery, Selectors & Core Navigation:**
  - `actions/_phone_link_driver.py`, `actions/phone_link.py`, `config/phone_link_selectors.json`, feature toggles in `memory/config_manager.py` & UI settings toggles, `status`, `launch`, `open_feature`, `diagnose`, fake-UIA unit tests.
- **Phase 2 — Notifications & Phone Controls:**
  - `list_notifications`, `dismiss_notification`, `clear_all`, `dnd`, `mute_phone` (with `core/undo.py`), unit tests.
- **Phase 3 — Messages (SMS & Optional MMS):**
  - `list_conversations`, `read_conversation`, `send_sms` (with thread re-read verification), `send_mms_attachment` (folder allowlist), OTP redaction, unit tests.
- **Phase 4 — Calls:**
  - `recent_calls`, `call` (emergency block + confirmation + active-call verification), `answer`, `decline`, `end_call`, `dial_digits`, `call_status`, experimental `dial_via_tel_uri`, unit tests.
- **Phase 5 — Photos, Apps & Optional Watcher:**
  - `list_recent_photos`, `copy_photo`, `save_photo`, disabled-by-default `share_photo`/`delete_photo`, `list_apps`, `open_app`, `open_phone_screen`, plus opt-in low-CPU UIA polling watcher, unit tests.
- **Phase 6 — Hardening, Multi-Locale & Documentation:**
  - Multi-locale selector tests, timeout/resilience hardening, selector re-recording guide, `CHANGELOG.md`, `core/prompt.txt`, and real-device manual test checklist.
