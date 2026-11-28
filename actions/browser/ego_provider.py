"""
actions/browser/ego_provider.py — Ego(lite) Browser Engine Provider.

Controls Ego Lite Chromium browser via its native Node.js automation harness (ego-browser).
Supports isolated task Spaces, semantic snapshots, durable element references (@N),
and shared authentication states without disrupting the user's active desktop windows.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from actions.browser.base_provider import BrowserProvider
from actions.browser.browser_models import BrowserStatus, ElementRef, SpaceInfo, TabInfo

logger = logging.getLogger("ego_provider")


def locate_ego_executable() -> Optional[str]:
    """Find ego-browser executable on the system."""
    which_path = shutil.which("ego-browser")
    if which_path:
        return str(Path(which_path).resolve())

    # Windows standard locations
    if platform.system() == "Windows":
        candidates = [
            Path(os.environ.get("LOCALAPPDATA", "")) / ".." / ".local" / "share" / "ego" / "active_version_dir" / "ego-browser.exe",
            Path.home() / ".local" / "share" / "ego" / "active_version_dir" / "ego-browser.exe",
            Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "Ego" / "ego-browser.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "ego" / "ego-browser.exe",
        ]
        for c in candidates:
            try:
                res = c.resolve()
                if res.is_file():
                    return str(res)
            except Exception:
                continue

    return None


class EgoLiteProvider(BrowserProvider):
    """
    Ego(lite) Browser Automation Engine.
    Executes automation within isolated task Spaces with semantic DOM perception.
    """

    def __init__(self, executable_path: Optional[str] = None):
        self._exe = executable_path or locate_ego_executable()
        self._active_space_id: Optional[int | str] = None
        self._active_space_name: str = "JARVIS Workspace"
        self._active_page_label: str = "p1"
        self._last_url: str = ""
        self._last_title: str = ""
        self._last_raw_snapshot: str = ""
        self._element_map: Dict[int, ElementRef] = {}
        self._lock = threading.Lock()
        self._last_error: Optional[str] = None

    @property
    def name(self) -> str:
        return "ego"

    @property
    def executable(self) -> Optional[str]:
        return self._exe

    @property
    def is_connected(self) -> bool:
        ok, _ = self.check_health(timeout=2.0)
        return ok

    @property
    def active_page_url(self) -> Optional[str]:
        return self._last_url or None

    @property
    def active_page_title(self) -> Optional[str]:
        return self._last_title or None

    @property
    def active_space_id(self) -> Optional[int | str]:
        return self._active_space_id

    # ── Script Execution Core ─────────────────────────────────────────────────

    def _run_script(self, js_body: str, timeout: float = 25.0) -> Tuple[int, str, str]:
        """
        Execute JavaScript inside the ego-browser Node.js runtime.
        Returns (exit_code, stdout, stderr).
        """
        if not self._exe:
            self._exe = locate_ego_executable()
        if not self._exe:
            err = "Ego(lite) binary (ego-browser) was not found on this machine."
            self._last_error = err
            return -1, "", err

        with self._lock:
            try:
                proc = subprocess.Popen(
                    [self._exe, "nodejs"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                stdout, stderr = proc.communicate(js_body, timeout=timeout)
                if proc.returncode != 0:
                    self._last_error = stderr.strip() or f"Process exited with code {proc.returncode}"
                return proc.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                err = f"Ego(lite) command timed out after {timeout:.1f}s."
                self._last_error = err
                return -1, "", err
            except Exception as e:
                err = f"Failed to execute ego-browser: {e}"
                self._last_error = err
                return -1, "", err

    def _build_space_preamble(self) -> str:
        """Construct standard JavaScript preamble to bind active task space and page."""
        space_repr = json.dumps(self._active_space_id) if self._active_space_id is not None else "null"
        name_repr = json.dumps(self._active_space_name)
        return f"""
const TARGET_SPACE_ID = {space_repr};
const TARGET_SPACE_NAME = {name_repr};
const allSpaces = await listTaskSpaces();
let task = null;

if (TARGET_SPACE_ID !== null) {{
    const existing = allSpaces.find(s => s.id === TARGET_SPACE_ID);
    if (existing) {{
        if (existing.ownership === "user") {{
            task = await claimTaskSpace(TARGET_SPACE_ID);
        }} else if (existing.ownership === "agentDelegatedToUser") {{
            task = await takeOverTaskSpace(TARGET_SPACE_ID);
        }} else {{
            task = await taskSpace(TARGET_SPACE_ID);
        }}
    }}
}}

if (!task) {{
    const agentSpace = allSpaces.find(s => s.ownership === "agent" && s.name === TARGET_SPACE_NAME);
    if (agentSpace) {{
        task = await taskSpace(agentSpace.id);
    }} else {{
        task = await taskSpace(TARGET_SPACE_NAME);
    }}
}}

const activeSpaceId = task.spaceId;
const pages = await task.pages();
const page = pages[0] ?? (await task.tabs())[0]?.page ?? await task.newPage();
"""

    # ── Health & Status ───────────────────────────────────────────────────────

    def check_health(self, timeout: float = 2.0) -> Tuple[bool, str]:
        if not self._exe:
            self._exe = locate_ego_executable()
        if not self._exe or not Path(self._exe).is_file():
            return False, f"Ego(lite) executable (ego-browser) was not found at '{self._exe or 'PATH'}'."

        script = """
try {
    const spaces = await listTaskSpaces();
    console.log(JSON.stringify({ ok: true, spaceCount: spaces.length }));
} catch (e) {
    console.log(JSON.stringify({ ok: false, error: String(e) }));
}
"""
        code, out, err = self._run_script(script, timeout=timeout)
        combined = (out + "\n" + err).strip()
        if code == 0 and '"ok":true' in combined:
            return True, "Ego(lite) browser is running and responsive."
        return False, f"Ego(lite) health check failed: {err.strip() or combined}"

    def get_status(self) -> BrowserStatus:
        script = """
try {
    const spaces = await listTaskSpaces();
    let currentTabs = [];
    let curTitle = "";
    let curUrl = "";
    let curSpaceId = null;

    if (spaces.length > 0) {
        const agentS = spaces.find(s => s.ownership === "agent") || spaces[0];
        curSpaceId = agentS.id;
        try {
            const t = await taskSpace(agentS.id);
            const tb = await t.tabs();
            currentTabs = tb.map((tab, idx) => ({
                index: idx,
                title: tab.title || "Tab",
                url: tab.url || "",
                targetId: tab.targetId || "",
                isActive: idx === 0
            }));
            const p = (await t.pages())[0];
            if (p) {
                curTitle = await p.title();
                curUrl = await p.evaluate("window.location.href");
            }
        } catch (_) {}
    }

    console.log(JSON.stringify({
        ok: true,
        spaces,
        activeSpaceId: curSpaceId,
        activeTitle: curTitle,
        activeUrl: curUrl,
        tabs: currentTabs
    }));
} catch (err) {
    console.log(JSON.stringify({ ok: false, error: String(err) }));
}
"""
        code, out, err = self._run_script(script, timeout=6.0)
        combined = (out + "\n" + err).strip()

        space_objs: List[SpaceInfo] = []
        tab_objs: List[TabInfo] = []
        is_conn = False
        cur_space = self._active_space_id
        cur_title = self._last_title
        cur_url = self._last_url

        try:
            for line in combined.splitlines():
                line = line.strip()
                if line.startswith("{") and line.endswith("}"):
                    data = json.loads(line)
                    if data.get("ok"):
                        is_conn = True
                        cur_space = data.get("activeSpaceId")
                        cur_title = data.get("activeTitle") or cur_title
                        cur_url = data.get("activeUrl") or cur_url
                        for sp in data.get("spaces", []):
                            space_objs.append(
                                SpaceInfo(
                                    id=sp.get("id"),
                                    name=sp.get("name") or f"Space {sp.get('id')}",
                                    ownership=sp.get("ownership", "user"),
                                    profile_id=sp.get("profileId", ""),
                                    profile_name=sp.get("profileName", ""),
                                    is_active=(sp.get("id") == cur_space),
                                    recent_titles=sp.get("recentTabTitles", []),
                                )
                            )
                        for tb in data.get("tabs", []):
                            tab_objs.append(
                                TabInfo(
                                    index=tb.get("index", 0),
                                    title=tb.get("title", ""),
                                    url=tb.get("url", ""),
                                    target_id=tb.get("targetId", ""),
                                    is_active=tb.get("isActive", False),
                                )
                            )
                        break
        except Exception as e:
            logger.debug(f"Error parsing status JSON: {e}")

        return BrowserStatus(
            provider="ego",
            is_connected=is_conn,
            version="ego-lite v2.2.1",
            active_space=cur_space,
            active_title=cur_title,
            active_url=cur_url,
            tab_count=len(tab_objs),
            tabs=tab_objs,
            health="healthy" if is_conn else "disconnected",
            last_error=self._last_error,
            spaces=space_objs,
        )

    # ── Observation & Semantic Snapshot ───────────────────────────────────────

    def _parse_snapshot(self, raw_snap: str) -> List[ElementRef]:
        """
        Parse Ego Lite snapshot text into indexed ElementRef items.
        Ego snapshot lines with references look like:
          anchor [ref=1, loc=href:..., url=...] text "Learn more"
          button [ref=2, loc=...] text "Submit"
          textbox [ref=3, loc=...] placeholder "Search"
          input [ref=4, loc=...] value "foo"
        """
        elements: List[ElementRef] = []
        if not raw_snap:
            return elements

        # Regex to capture role/tag, ref ID, optional loc/attributes, and optional text
        pattern = re.compile(
            r'^\s*([a-zA-Z0-9_\-]+)\s+\[ref=(\d+)(?:,\s*([^\]]*))?\](?:\s+text\s+"([^"]*)")?',
            re.MULTILINE,
        )

        for match in pattern.finditer(raw_snap):
            role_tag = match.group(1).strip()
            ref_id = int(match.group(2))
            meta_str = match.group(3) or ""
            text_val = match.group(4) or ""

            placeholder = ""
            aria_label = ""
            name_val = ""
            val = ""

            for part in meta_str.split(","):
                part = part.strip()
                if part.startswith("loc="):
                    pass
                elif part.startswith("placeholder="):
                    placeholder = part.split("=", 1)[1].strip('"\'')
                elif part.startswith("aria-label="):
                    aria_label = part.split("=", 1)[1].strip('"\'')
                elif part.startswith("name="):
                    name_val = part.split("=", 1)[1].strip('"\'')
                elif part.startswith("value="):
                    val = part.split("=", 1)[1].strip('"\'')

            elem = ElementRef(
                index=ref_id,
                role=role_tag,
                tag=role_tag,
                text=text_val,
                name=name_val,
                aria_label=aria_label,
                placeholder=placeholder,
                value=val,
                raw_ref=f"@{ref_id}",
            )
            elements.append(elem)

        return elements

    def observe(self, enable_som: bool = False) -> str:
        script = f"""
{self._build_space_preamble()}
const snap = await page.snapshot({{ scope: "only_within_viewport", includeActionMarks: true, includeStableLocator: true }});
const title = await page.title();
const url = await page.evaluate("window.location.href");

console.log("===EGO_OBS_START===");
console.log(JSON.stringify({{
    spaceId: activeSpaceId,
    title,
    url,
    snap
}}));
console.log("===EGO_OBS_END===");
"""
        code, out, err = self._run_script(script, timeout=12.0)
        combined = out + "\n" + err

        if "===EGO_OBS_START===" in combined and "===EGO_OBS_END===" in combined:
            start = combined.find("===EGO_OBS_START===") + len("===EGO_OBS_START===")
            end = combined.find("===EGO_OBS_END===")
            block = combined[start:end].strip()
            try:
                data = json.loads(block)
                self._active_space_id = data.get("spaceId")
                self._last_title = data.get("title", "")
                self._last_url = data.get("url", "")
                raw_snap = data.get("snap", "")
                self._last_raw_snapshot = raw_snap

                elements = self._parse_snapshot(raw_snap)
                self._element_map = {e.index: e for e in elements}

                lines = [e.to_prompt_line() for e in elements]
                elements_block = "\n".join(lines) if lines else "(No interactive elements identified in viewport)"

                return (
                    f"Page: '{self._last_title}' ({self._last_url})\n"
                    f"Interactive Elements (Ego Space #{self._active_space_id}):\n"
                    f"{elements_block}\n\n"
                    f"Semantic Page Structure:\n{raw_snap}"
                )
            except Exception as e:
                logger.debug(f"Error parsing observation JSON: {e}")

        if code != 0:
            return f"[Ego Error] Observation failed: {err.strip() or 'Unknown error'}"
        return f"Observation completed:\n{combined.strip()}"

    # ── Navigation & Interactions ─────────────────────────────────────────────

    def navigate(self, url: str) -> str:
        url_clean = url.strip()
        if not url_clean.startswith(("http://", "https://", "about:", "file://")):
            url_clean = f"https://{url_clean}"

        url_repr = json.dumps(url_clean)
        script = f"""
{self._build_space_preamble()}
await page.goto({url_repr}, {{ waitUntil: "domcontentloaded", timeout: 15000 }});
const title = await page.title();
const curUrl = await page.evaluate("window.location.href");
console.log("===NAV_RESULT===");
console.log(JSON.stringify({{ spaceId: activeSpaceId, title, url: curUrl }}));
"""
        code, out, err = self._run_script(script, timeout=20.0)
        combined = out + "\n" + err

        if "===NAV_RESULT===" in combined:
            try:
                line = combined.split("===NAV_RESULT===")[1].strip().splitlines()[0]
                data = json.loads(line)
                self._active_space_id = data.get("spaceId")
                self._last_title = data.get("title", "")
                self._last_url = data.get("url", "")
                return f"Navigated to '{self._last_url}' (Title: '{self._last_title}') [Ego Space #{self._active_space_id}]."
            except Exception:
                pass

        if code == 0:
            self._last_url = url_clean
            return f"Navigated to '{url_clean}' in Ego Space #{self._active_space_id or 'main'}."
        return f"[Ego Error] Navigation failed: {err.strip() or combined.strip()}"

    def click(
        self,
        index: Optional[int] = None,
        text: Optional[str] = None,
        selector: Optional[str] = None,
    ) -> str:
        # Determine click target selector / ref
        target = ""
        if index is not None:
            target = f"@{index}"
        elif text:
            clean_text = text.strip().lower()
            # Try to match in element map
            matched_ref = None
            for idx, elem in self._element_map.items():
                if clean_text in (elem.text.lower(), elem.aria_label.lower(), elem.name.lower()):
                    matched_ref = idx
                    break
            if matched_ref is not None:
                target = f"@{matched_ref}"
            else:
                target = f"text={text.strip()}"
        elif selector:
            target = selector.strip()
        else:
            return "[Error] Provide 'index' or 'text' for click action."

        target_repr = json.dumps(target)
        script = f"""
{self._build_space_preamble()}
await page.click({target_repr}, {{ timeout: 8000 }});
await new Promise(r => setTimeout(r, 400));
const title = await page.title();
const url = await page.evaluate("window.location.href");
console.log("===CLICK_RESULT===");
console.log(JSON.stringify({{ spaceId: activeSpaceId, title, url }}));
"""
        code, out, err = self._run_script(script, timeout=15.0)
        combined = out + "\n" + err

        if "===CLICK_RESULT===" in combined:
            try:
                line = combined.split("===CLICK_RESULT===")[1].strip().splitlines()[0]
                data = json.loads(line)
                self._last_title = data.get("title", "")
                self._last_url = data.get("url", "")
                obs = self.observe()
                return f"Clicked {target}.\n{obs}"
            except Exception:
                pass

        if code == 0:
            obs = self.observe()
            return f"Clicked {target}.\n{obs}"
        return f"[Ego Error] Click on {target} failed: {err.strip() or combined.strip()}"

    def type_text(
        self,
        text: str,
        index: Optional[int] = None,
        selector: Optional[str] = None,
        press_enter: bool = False,
    ) -> str:
        target = ""
        if index is not None:
            target = f"@{index}"
        elif selector:
            target = selector.strip()
        else:
            # Fallback to active focused element
            target = ":focus"

        target_repr = json.dumps(target)
        text_repr = json.dumps(text)
        press_repr = "true" if press_enter else "false"

        script = f"""
{self._build_space_preamble()}
await page.fill({target_repr}, {text_repr}, {{ clearFirst: true, timeout: 8000 }});
if ({press_repr}) {{
    await page.press({target_repr}, "Enter");
}}
await new Promise(r => setTimeout(r, 400));
const title = await page.title();
const url = await page.evaluate("window.location.href");
console.log("===TYPE_RESULT===");
console.log(JSON.stringify({{ spaceId: activeSpaceId, title, url }}));
"""
        code, out, err = self._run_script(script, timeout=15.0)
        combined = out + "\n" + err

        if "===TYPE_RESULT===" in combined:
            try:
                line = combined.split("===TYPE_RESULT===")[1].strip().splitlines()[0]
                data = json.loads(line)
                self._last_title = data.get("title", "")
                self._last_url = data.get("url", "")
                obs = self.observe()
                return f"Typed '{text}' into {target}" + (" with Enter.\n" if press_enter else ".\n") + obs
            except Exception:
                pass

        if code == 0:
            obs = self.observe()
            return f"Typed '{text}' into {target}.\n{obs}"
        return f"[Ego Error] Typing into {target} failed: {err.strip() or combined.strip()}"

    def press_key(self, key: str) -> str:
        key_clean = key.strip()
        key_repr = json.dumps(key_clean)
        script = f"""
{self._build_space_preamble()}
await page.keyboard.press({key_repr});
await new Promise(r => setTimeout(r, 300));
console.log("KEY_PRESSED");
"""
        code, out, err = self._run_script(script, timeout=8.0)
        if code == 0 or "KEY_PRESSED" in (out + err):
            return f"Pressed key '{key_clean}' in Ego Space #{self._active_space_id}."
        return f"[Ego Error] Pressing key '{key_clean}' failed: {err.strip() or out.strip()}"

    def scroll(
        self,
        direction: str = "down",
        amount: int = 500,
        index: Optional[int] = None,
    ) -> str:
        delta = -amount if direction.lower() == "up" else amount
        script = f"""
{self._build_space_preamble()}
await page.mouse.wheel(0, {delta});
await new Promise(r => setTimeout(r, 300));
console.log("SCROLLED");
"""
        code, out, err = self._run_script(script, timeout=8.0)
        if code == 0 or "SCROLLED" in (out + err):
            return f"Scrolled page {direction} by {amount}px [Ego Space #{self._active_space_id}]."
        return f"[Ego Error] Scroll failed: {err.strip() or out.strip()}"

    def screenshot(self, path: str = "browser_screenshot.png") -> str:
        abs_path = str(Path(path).expanduser().resolve()).replace("\\", "/")
        path_repr = json.dumps(abs_path)
        script = f"""
{self._build_space_preamble()}
await page.screenshot({{ path: {path_repr} }});
console.log("SCREENSHOT_OK");
"""
        code, out, err = self._run_script(script, timeout=12.0)
        if code == 0 or "SCREENSHOT_OK" in (out + err):
            return f"Saved screenshot to {abs_path}."
        return f"[Ego Error] Screenshot failed: {err.strip() or out.strip()}"

    def new_tab(self, url: str = "https://google.com", index: Optional[int] = None) -> TabInfo | str:
        url_clean = url.strip() if url else "https://google.com"
        url_repr = json.dumps(url_clean)
        script = f"""
{self._build_space_preamble()}
const newPage = await task.newPage();
if ({url_repr}) {{
    await newPage.goto({url_repr}, {{ waitUntil: "domcontentloaded", timeout: 12000 }});
}}
const tabs = await task.tabs();
const title = await newPage.title();
const curUrl = await newPage.evaluate("window.location.href");
console.log("===NEW_TAB_RESULT===");
console.log(JSON.stringify({{
    index: tabs.length - 1,
    title,
    url: curUrl,
    spaceId: activeSpaceId
}}));
"""
        code, out, err = self._run_script(script, timeout=18.0)
        combined = out + "\n" + err

        if "===NEW_TAB_RESULT===" in combined:
            try:
                line = combined.split("===NEW_TAB_RESULT===")[1].strip().splitlines()[0]
                data = json.loads(line)
                return TabInfo(
                    index=data.get("index", 0),
                    title=data.get("title", "New Tab"),
                    url=data.get("url", url_clean),
                    is_active=True,
                )
            except Exception:
                pass

        if code == 0:
            return TabInfo(index=0, title="New Tab", url=url_clean, is_active=True)
        return f"[Ego Error] New tab creation failed: {err.strip() or combined.strip()}"

    def switch_tab(self, tab_index: int) -> str:
        script = f"""
{self._build_space_preamble()}
const allTabs = await task.tabs();
const targetIdx = {int(tab_index)};
if (targetIdx >= 0 && targetIdx < allTabs.length) {{
    const targetTab = allTabs[targetIdx];
    const p = task.page(targetTab.label || "p1");
    await p.focus?.().catch(() => {{}});
    console.log(JSON.stringify({{ ok: true, title: targetTab.title, url: targetTab.url }}));
}} else {{
    console.log(JSON.stringify({{ ok: false, error: "Tab index out of range" }}));
}}
"""
        code, out, err = self._run_script(script, timeout=8.0)
        combined = (out + "\n" + err).strip()
        for line in combined.splitlines():
            if line.startswith("{") and line.endswith("}"):
                try:
                    data = json.loads(line)
                    if data.get("ok"):
                        return f"Switched to tab [{tab_index}] '{data.get('title')}' in Ego Space #{self._active_space_id}."
                    return f"[Error] {data.get('error')}"
                except Exception:
                    pass
        return f"Switched tab to index {tab_index} in Ego Space #{self._active_space_id}."

    def close_tab(self, tab_index: Optional[int] = None) -> str:
        script = f"""
{self._build_space_preamble()}
await page.close();
console.log("CLOSED_PAGE");
"""
        code, out, err = self._run_script(script, timeout=8.0)
        if code == 0 or "CLOSED_PAGE" in (out + err):
            return f"Closed active tab in Ego Space #{self._active_space_id}."
        return f"[Ego Error] Closing tab failed: {err.strip() or out.strip()}"

    def list_tabs(self) -> List[TabInfo]:
        script = f"""
{self._build_space_preamble()}
const tb = await task.tabs();
console.log("===TABS_LIST===");
console.log(JSON.stringify(tb.map((t, idx) => ({{
    index: idx,
    title: t.title || "Tab",
    url: t.url || "",
    targetId: t.targetId || "",
    isActive: idx === 0
}}))));
"""
        code, out, err = self._run_script(script, timeout=8.0)
        combined = out + "\n" + err
        tabs: List[TabInfo] = []

        if "===TABS_LIST===" in combined:
            try:
                line = combined.split("===TABS_LIST===")[1].strip().splitlines()[0]
                raw_list = json.loads(line)
                for t in raw_list:
                    tabs.append(
                        TabInfo(
                            index=t.get("index", 0),
                            title=t.get("title", ""),
                            url=t.get("url", ""),
                            target_id=t.get("targetId", ""),
                            is_active=t.get("isActive", False),
                        )
                    )
                return tabs
            except Exception:
                pass
        return tabs

    def go_back(self) -> str:
        script = f"""
{self._build_space_preamble()}
await page.evaluate("window.history.back()");
await new Promise(r => setTimeout(r, 400));
const title = await page.title();
const url = await page.evaluate("window.location.href");
console.log(JSON.stringify({{ title, url }}));
"""
        code, out, err = self._run_script(script, timeout=8.0)
        return f"Navigated back in Ego Space #{self._active_space_id}."

    def wait_page(self, seconds: float = 2.0) -> str:
        sec = max(0.1, min(float(seconds), 30.0))
        script = f"""
{self._build_space_preamble()}
await new Promise(r => setTimeout(r, {int(sec * 1000)}));
console.log("WAITED");
"""
        self._run_script(script, timeout=sec + 5.0)
        return f"Waited {sec:.1f}s in Ego Space #{self._active_space_id}."

    def open_browser(self, headed: bool = True) -> bool:
        """Launch or wake Ego Lite app."""
        if not self._exe:
            self._exe = locate_ego_executable()
        if not self._exe:
            return False

        try:
            # Running listTaskSpaces ensures Ego Lite background app is initialized
            ok, _ = self.check_health(timeout=3.0)
            if ok:
                return True
            # Spawn ego-browser directly
            subprocess.Popen([self._exe], shell=False)
            return True
        except Exception as e:
            logger.error(f"Failed to open Ego Lite browser: {e}")
            return False

    def disconnect(self) -> None:
        """Release active task space."""
        if self._active_space_id is not None:
            self.close_space(self._active_space_id)
            self._active_space_id = None

    # ── Task Spaces Management ────────────────────────────────────────────────

    def create_space(self, name: str) -> SpaceInfo | str:
        clean_name = name.strip() or "JARVIS Space"
        name_repr = json.dumps(clean_name)
        script = f"""
const task = await taskSpace({name_repr});
console.log("===SPACE_CREATED===");
console.log(JSON.stringify({{
    id: task.spaceId,
    name: task.name,
    ownership: task.ownership
}}));
"""
        code, out, err = self._run_script(script, timeout=10.0)
        combined = out + "\n" + err

        if "===SPACE_CREATED===" in combined:
            try:
                line = combined.split("===SPACE_CREATED===")[1].strip().splitlines()[0]
                data = json.loads(line)
                new_id = data.get("id")
                self._active_space_id = new_id
                self._active_space_name = clean_name
                return SpaceInfo(
                    id=new_id,
                    name=data.get("name", clean_name),
                    ownership=data.get("ownership", "agent"),
                    is_active=True,
                )
            except Exception as e:
                logger.debug(f"Error parsing space creation JSON: {e}")

        if code == 0:
            return f"Created Ego task space '{clean_name}'."
        return f"[Ego Error] Could not create space '{clean_name}': {err.strip() or out.strip()}"

    def switch_space(self, space_id: int | str) -> str:
        try:
            target_id = int(space_id)
        except ValueError:
            target_id = space_id

        self._active_space_id = target_id
        return f"Switched active Ego workspace to Space #{target_id}."

    def close_space(self, space_id: Optional[int | str] = None) -> str:
        target_id = space_id if space_id is not None else self._active_space_id
        if target_id is None:
            return "No active space to close."

        id_repr = json.dumps(target_id)
        script = f"""
const spaces = await listTaskSpaces();
const sEntry = spaces.find(s => s.id === {id_repr});
if (sEntry) {{
    const task = await taskSpace({id_repr});
    await task.finish({{ keep: [] }});
    console.log("SPACE_CLOSED");
}} else {{
    console.log("SPACE_NOT_FOUND");
}}
"""
        code, out, err = self._run_script(script, timeout=8.0)
        if self._active_space_id == target_id:
            self._active_space_id = None
        return f"Closed Ego task space #{target_id}."

    def list_spaces(self) -> List[SpaceInfo]:
        script = """
const spaces = await listTaskSpaces();
console.log("===SPACES_LIST===");
console.log(JSON.stringify(spaces));
"""
        code, out, err = self._run_script(script, timeout=8.0)
        combined = out + "\n" + err
        spaces: List[SpaceInfo] = []

        if "===SPACES_LIST===" in combined:
            try:
                line = combined.split("===SPACES_LIST===")[1].strip().splitlines()[0]
                raw_list = json.loads(line)
                for sp in raw_list:
                    spaces.append(
                        SpaceInfo(
                            id=sp.get("id"),
                            name=sp.get("name") or f"Space {sp.get('id')}",
                            ownership=sp.get("ownership", "user"),
                            profile_id=sp.get("profileId", ""),
                            profile_name=sp.get("profileName", ""),
                            is_active=(sp.get("id") == self._active_space_id),
                            recent_titles=sp.get("recentTabTitles", []),
                        )
                    )
            except Exception:
                pass
        return spaces
