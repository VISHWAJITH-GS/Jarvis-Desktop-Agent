# Phone Link UIA Dumps (`docs/phone_link_uia_dumps/`)

## Discovery Environment Notes
- **OS Locale / Display Language:** `en_US` (`0x409`)
- **Installed Package:** `Microsoft.YourPhone` (`Microsoft.YourPhone_8wekyb3d8bbwe`, Status: `Ok`)
- **Background Process Observed:** `PhoneExperienceHost.exe` (PID observed via `psutil`)
- **Interactive UIA Capture Status:** Automated UIA window enumeration from the agent's command runner returned `0` top-level desktop windows because the runner executes outside the interactive user desktop session (`WinSta0\Default`). Therefore, **no live UIA trees could be captured automatically** and no placeholder/guessed `AutomationId`s are fabricated here.

## How to Capture Redacted UIA Dumps Locally
With **Phone Link** open on your desktop and your phone linked, run a local Python walker from an interactive terminal for each page (**Calls**, **Messages**, **Notifications**, **Photos**, **Apps**, **Settings**) and save the redacted outputs here as:
- `calls.txt`
- `messages.txt`
- `notifications.txt`
- `photos.txt`
- `apps.txt`
- `settings.txt`

Each dump must record:
1. Top-level window `Title`, `ClassName`, `ProcessName`, and Windows display language.
2. For each UIA control: `ControlType`, `AutomationId`, `Name` (with **all personal names, phone numbers, message bodies, and email addresses replaced with `<REDACTED>`**).
