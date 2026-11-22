"""
Action discovery, validation, and dispatch — the built-in twin of plugin_loader.

Every actions/*.py that exposes a module-level ``TOOL`` dict is auto-discovered
here, exactly like a drop-in plugin, so main.py never has to hardcode a tool
declaration or a dispatch branch for it. Adding a new bundled action is then the
same one-file operation as writing a plugin: define ``TOOL`` and a handler.

``TOOL`` shape (see actions/open_app.py for a live example):

    TOOL = {
        "name":        "open_app",              # unique, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
        "description":  "...",                   # what Gemini reads to route the call
        "parameters":  {"type": "OBJECT", ...}, # Gemini function-declaration schema
        "handler":      open_app,                # the callable to run
    }

The handler is invoked through signature introspection: it receives ``parameters``
plus whichever of ``player`` / ``speak`` / ``response`` / ``session_memory`` it
actually declares — so existing action signatures work unchanged.

Discovery runs once at startup; import errors, validation errors, and name
collisions are logged and the offending file is skipped — they NEVER raise out
of discover_actions() and never abort the scan of the remaining files.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import re
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional
import time
from core import outcome
from core.schema_utils import (
    _BEHAVIORS,
    _SCHEDULING,
    META_KEYS,
    strip_meta,
    _opt_upper,
    _opt_tier,
    _opt_text,
    validate_parameters,
)

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
_DEFAULT_PARAMS = {"type": "OBJECT", "properties": {}}
_CTX_KEYS = ("player", "speak", "response", "session_memory")


class LazyActionHandler:
    """Lazy callable proxy for an action handler.
    Defers importing the handler's module until the action is first invoked."""

    def __init__(self, action_name: str, module_name: str, func_name: str, file_name: str):
        self.action_name = action_name
        self.module_name = module_name
        self.func_name = func_name
        self.file_name = file_name
        self._target_fn: Optional[Callable] = None
        self._load_error: Optional[str] = None

    def resolve(self) -> Callable:
        if self._target_fn is not None:
            return self._target_fn
        if self._load_error is not None:
            raise ImportError(self._load_error)
        try:
            mod = importlib.import_module(self.module_name)
            fn = getattr(mod, self.func_name)
            self._target_fn = fn
            return fn
        except Exception as e:
            self._load_error = f"Failed to load action '{self.action_name}' from {self.file_name}: {e}"
            raise ImportError(self._load_error) from e

    def __call__(self, *args, **kwargs):
        fn = self.resolve()
        return fn(*args, **kwargs)

    def __getattr__(self, name):
        if name in ("action_name", "module_name", "func_name", "file_name", "_target_fn", "_load_error", "resolve"):
            return super().__getattribute__(name)
        fn = self.resolve()
        return getattr(fn, name)

    def __repr__(self) -> str:
        return f"<LazyActionHandler for {self.action_name} ({self.module_name}.{self.func_name})>"


@dataclass
class ActionRecord:
    name: str
    description: str = ""
    parameters: dict = field(default_factory=lambda: dict(_DEFAULT_PARAMS))
    handler: Optional[Callable] = None
    file: str = ""
    valid: bool = False
    error: str = ""
    behavior: Optional[str] = None  # None = the API's default (blocking)
    scheduling: Optional[str] = None  # None = the API's default (WHEN_IDLE)
    # Read by providers/schema.py when a small model gets a tool budget; never
    # sent to any API — main.py strips both before the Gemini config is built.
    #   tier   "core" | "extra" — this file's own opinion of whether it should
    #          survive a tight budget, instead of that being decided by the
    #          order files happen to be discovered in.
    #   brief  a short description used in place of the long one when a budget
    #          is in force. Nothing is deleted: Gemini still gets the full text.
    tier: Optional[str] = None
    brief: Optional[str] = None
    permission: str = "normal"  # "read_only" | "normal" | "sensitive" | "destructive"

    def validate_parameters(self, parameters: dict) -> tuple[bool, str]:
        schema = self.parameters or {}
        required = schema.get("required", [])
        for req in required:
            if req not in parameters or parameters[req] is None or parameters[req] == "":
                return False, f"Missing required parameter '{req}'"
        return True, ""


class ActionRegistry:
    def __init__(self, actions: dict[str, ActionRecord], logger: Callable[[str], None]):
        self._actions = actions  # name -> ActionRecord, VALID entries only
        self._all_records: list[ActionRecord] = []
        self._logger = logger

    # -- called by main.py at LiveConnectConfig build time --
    def get_tool_declarations(self) -> list[dict]:
        out = []
        for rec in self._actions.values():
            decl = {"name": rec.name, "description": rec.description, "parameters": rec.parameters}
            if rec.behavior:
                decl["behavior"] = rec.behavior
            # Budget metadata. NOT part of any API's schema — main.py calls
            # providers.schema.strip_meta() before the Gemini config is built,
            # and budget() consumes them on the local-engine path.
            if rec.tier:
                decl["tier"] = rec.tier
            if rec.brief:
                decl["brief"] = rec.brief
            out.append(decl)
        return out

    def has_action(self, name: str) -> bool:
        """Stable public API: returns True if action is registered and valid."""
        return name in self._actions

    def has(self, name: str) -> bool:
        return self.has_action(name)

    def get_declaration(self, name: str) -> Optional[dict]:
        """Stable public API: returns tool declaration dict (name, description, parameters, tier, etc.) or None."""
        rec = self._actions.get(name)
        if rec is None:
            return None
        decl = {
            "name": rec.name,
            "description": rec.description,
            "parameters": rec.parameters or dict(_DEFAULT_PARAMS),
        }
        if rec.tier:
            decl["tier"] = rec.tier
        if rec.brief:
            decl["brief"] = rec.brief
        return decl

    def get_parameters_schema(self, name: str) -> Optional[dict]:
        """Stable public API: returns parameters schema dictionary for action or None."""
        rec = self._actions.get(name)
        if rec is None:
            return None
        return rec.parameters or dict(_DEFAULT_PARAMS)

    def list_actions(self) -> list[str]:
        """Stable public API: returns sorted list of registered action names."""
        return sorted(self._actions.keys())

    def scheduling(self, name: str) -> Optional[str]:
        """How this action's result should re-enter the conversation, if it said."""
        rec = self._actions.get(name)
        return rec.scheduling if rec else None

    def names(self) -> set[str]:
        return set(self._actions.keys())

    def get_action_record(self, name: str) -> Optional[ActionRecord]:
        return self._actions.get(name)

    def get_action(self, name: str) -> Optional[ActionRecord]:
        """Stable public API: returns ActionRecord for action or None."""
        return self._actions.get(name)

    def _validate_parameters(self, rec: ActionRecord, parameters: dict) -> tuple[bool, str]:
        return validate_parameters(rec.parameters or {}, parameters or {})

    def execute(self, name: str, parameters: dict, ctx: dict | None = None) -> outcome.ActionResult:
        """Authoritative execution method returning structured ActionResult."""
        t0 = time.monotonic()
        rec = self._actions.get(name)
        if rec is None or not rec.valid:
            dur = (time.monotonic() - t0) * 1000
            return outcome.ActionResult(
                status=outcome.STATUS_UNSUPPORTED,
                action=name,
                success=False,
                verified=False,
                message=f"Action '{name}' is not available.",
                error=f"Action '{name}' is not registered or valid.",
                duration_ms=dur,
            )

        val_ok, val_err = self._validate_parameters(rec, parameters or {})
        if not val_ok:
            dur = (time.monotonic() - t0) * 1000
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action=name,
                success=False,
                verified=False,
                message=f"Invalid arguments for {name}: {val_err}",
                error=val_err,
                duration_ms=dur,
            )

        if rec.handler is None:
            return outcome.ActionResult(
                status=outcome.STATUS_FAILED,
                action=name,
                success=False,
                verified=False,
                message=f"Action '{name}' has no handler",
                error="No handler defined",
                duration_ms=(time.monotonic() - t0) * 1000.0,
            )

        # Resolve lazy handler on first execution and cache on the record
        if isinstance(rec.handler, LazyActionHandler):
            try:
                rec.handler = rec.handler.resolve()
            except Exception as e:
                dur = (time.monotonic() - t0) * 1000.0
                self._logger(f"Failed to load handler for '{name}': {e}")
                return outcome.ActionResult(
                    status=outcome.STATUS_FAILED,
                    action=name,
                    success=False,
                    verified=False,
                    message=f"Failed to load action '{name}': {e}",
                    error=str(e),
                    duration_ms=dur,
                )

        from core.permissions import ActionExecutionGuard

        return ActionExecutionGuard.guard_and_execute(
            action_name=name,
            parameters=parameters or {},
            handler=rec.handler,
            ctx=ctx or {},
            logger=self._logger,
            t0=t0,
        )

    # -- called by main.py from _execute_tool (backward-compatible string return) --
    def run(self, name: str, parameters: dict, ctx: dict | None = None) -> str:
        res = self.execute(name, parameters, ctx)
        return res.message or res.to_speech_text()


def _call_handler(fn: Callable, parameters: dict, ctx: dict) -> str:
    """Invoke the handler passing only the context kwargs it actually declares
    (or all of them if it has **kwargs), and validate arguments against callable parameters."""
    if isinstance(fn, LazyActionHandler):
        try:
            fn = fn.resolve()
        except Exception as e:
            from core import outcome
            return outcome.failed(getattr(fn, "action_name", "action"), str(e)).message or str(e)

    sig = inspect.signature(fn)
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())

    # Filter context arguments
    kwargs = {}
    for key in _CTX_KEYS:
        if has_var_kw or key in sig.parameters:
            kwargs[key] = ctx.get(key)

    # Some tools accept (parameters: dict, ...), while others accept direct kwargs (*args / **kwargs)
    if "parameters" in sig.parameters or has_var_kw:
        return fn(parameters=parameters, **kwargs)
    else:
        # If tool expects direct keyword arguments instead of a 'parameters' dict
        clean_args = {k: v for k, v in parameters.items() if has_var_kw or k in sig.parameters}
        return fn(**clean_args, **kwargs)


def _validate_tool(tool: dict, filename: str) -> ActionRecord:
    """Returns an ActionRecord; .valid=False + .error set on any problem. Never raises."""
    if not isinstance(tool, dict):
        return ActionRecord(name=Path(filename).stem, file=filename, error="TOOL must be a dict.")

    name = tool.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return ActionRecord(
            name=str(name or Path(filename).stem),
            file=filename,
            error="TOOL['name'] missing or not a valid identifier.",
        )

    description = tool.get("description")
    if not isinstance(description, str) or not description.strip():
        return ActionRecord(name=name, file=filename, error="TOOL['description'] missing or empty.")

    parameters = tool.get("parameters", _DEFAULT_PARAMS)
    if not isinstance(parameters, dict) or parameters.get("type") != "OBJECT":
        return ActionRecord(
            name=name, file=filename, error='TOOL[\'parameters\'] must be a dict with "type": "OBJECT".'
        )

    handler = tool.get("handler")
    if isinstance(handler, str):
        act_mod = f"actions.{Path(filename).stem}"
        if act_mod in sys.modules and hasattr(sys.modules[act_mod], handler):
            handler = getattr(sys.modules[act_mod], handler)
        else:
            handler = LazyActionHandler(name, act_mod, handler, filename)
    if not callable(handler):
        return ActionRecord(name=name, file=filename, error="TOOL['handler'] missing or not callable.")

    perm = tool.get("permission")
    if not perm:
        from core.permissions import ACTION_PERMISSIONS, SENSITIVE

        perm = ACTION_PERMISSIONS.get(name)
        if not perm:
            perm = SENSITIVE
    elif perm not in ("read_only", "normal", "sensitive", "destructive"):
        return ActionRecord(
            name=name,
            file=filename,
            error=f"TOOL['permission'] '{perm}' is invalid. Must be read_only, normal, sensitive, or destructive.",
        )

    return ActionRecord(
        name=name,
        description=description.strip(),
        parameters=parameters,
        handler=handler,
        file=filename,
        valid=True,
        error="",
        permission=perm,
        behavior=_opt_upper(tool.get("behavior"), _BEHAVIORS),
        scheduling=_opt_upper(tool.get("scheduling"), _SCHEDULING),
        tier=_opt_tier(tool.get("tier")),
        brief=_opt_text(tool.get("brief")),
    )


def _validate(module, filename: str) -> ActionRecord:
    """Legacy module validator: delegates to _validate_tool for getattr(module, 'TOOL')."""
    tool = getattr(module, "TOOL", None) or {}
    return _validate_tool(tool, filename)


def _eval_ast_node(node):
    if isinstance(node, ast.Constant):
        return node.value
    elif isinstance(node, ast.Dict):
        return {_eval_ast_node(k): _eval_ast_node(v) for k, v in zip(node.keys, node.values)}
    elif isinstance(node, ast.List):
        return [_eval_ast_node(x) for x in node.elts]
    elif isinstance(node, ast.Tuple):
        return tuple(_eval_ast_node(x) for x in node.elts)
    elif isinstance(node, ast.Name):
        return {"__ref__": node.id}
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_eval_ast_node(node.operand)
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _eval_ast_node(node.left)
        right = _eval_ast_node(node.right)
        if isinstance(left, str) and isinstance(right, str):
            return left + right
        elif isinstance(left, list) and isinstance(right, list):
            return left + right
    raise ValueError(f"Unsupported AST node: {ast.dump(node)}")


def _extract_tools_ast(path: Path) -> list[dict] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=str(path))
    except Exception:
        return None

    raw_tools = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("TOOL", "TOOLS"):
                    raw_tools.append((target.id, node.value))

    if not raw_tools:
        return []

    results = []
    module_name = f"actions.{path.stem}"
    for target_id, val_node in raw_tools:
        try:
            evaluated = _eval_ast_node(val_node)
            items = evaluated if isinstance(evaluated, list) else [evaluated]
            for it in items:
                if not isinstance(it, dict):
                    continue
                h_val = it.get("handler")
                func_name = ""
                if isinstance(h_val, dict) and "__ref__" in h_val:
                    func_name = h_val["__ref__"]
                elif isinstance(h_val, str):
                    func_name = h_val

                tool_copy = dict(it)
                act_name = tool_copy.get("name") or path.stem
                if func_name:
                    tool_copy["handler"] = LazyActionHandler(act_name, module_name, func_name, path.name)
                results.append(tool_copy)
        except Exception:
            return None  # Fall back to eager import if AST evaluation fails

    return results


def discover_actions(
    actions_dir: Path, reserved_names: set[str] | None = None, logger: Callable[[str], None] = print
) -> ActionRegistry:
    """
    Scans actions_dir for *.py files (skips files starting with '_'). A file is
    only treated as an action if it exposes a module-level TOOL dict or TOOLS list;
    files without one (shared helpers, capture-only modules) are silently ignored.
    Import/validation errors and name collisions are logged and the file is
    skipped — they NEVER raise out of this function.
    """
    reserved = reserved_names or set()
    actions_dir.mkdir(parents=True, exist_ok=True)
    valid: dict[str, ActionRecord] = {}
    all_records: list[ActionRecord] = []

    files = sorted(actions_dir.glob("*.py"), key=lambda p: p.name)  # deterministic order
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            module_name = f"actions.{path.stem}"
            tools_list: list[dict] = []

            # 1. If module already imported, reuse it directly
            if module_name in sys.modules:
                module = sys.modules[module_name]
                if hasattr(module, "TOOLS") and isinstance(module.TOOLS, list):
                    tools_list = [t for t in module.TOOLS if isinstance(t, dict)]
                elif hasattr(module, "TOOL") and isinstance(module.TOOL, dict):
                    tools_list = [module.TOOL]
                else:
                    continue
            else:
                # 2. Try zero-import static AST extraction first
                ast_tools = _extract_tools_ast(path)
                if ast_tools is not None:
                    if not ast_tools:
                        continue  # Helper / shared module without TOOL, skip without importing
                    tools_list = ast_tools
                else:
                    # 3. Fall back to loading module if static extraction failed
                    spec = importlib.util.spec_from_file_location(module_name, path)
                    if spec is None or spec.loader is None:
                        raise ImportError("could not build import spec")
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[module_name] = module
                    try:
                        spec.loader.exec_module(module)
                    except Exception:
                        sys.modules.pop(module_name, None)
                        raise

                    if hasattr(module, "TOOLS") and isinstance(module.TOOLS, list):
                        tools_list = [t for t in module.TOOLS if isinstance(t, dict)]
                    elif hasattr(module, "TOOL") and isinstance(module.TOOL, dict):
                        tools_list = [module.TOOL]
                    else:
                        continue

            for tool_dict in tools_list:
                rec = _validate_tool(tool_dict, path.name)

                if rec.valid and rec.name in reserved:
                    if rec.name == "screen_process":
                        # Register screen_processor as the working action handler for screen_process
                        pass
                    else:
                        rec = ActionRecord(
                            name=rec.name,
                            file=path.name,
                            error=f"Name '{rec.name}' collides with a reserved core tool — rejected.",
                        )
                elif rec.valid and rec.name in valid:
                    other = valid[rec.name].file
                    rec = ActionRecord(
                        name=rec.name,
                        file=path.name,
                        error=f"Name '{rec.name}' already used by action '{other}' — rejected.",
                    )

                all_records.append(rec)
                if rec.valid:
                    valid[rec.name] = rec
                    logger(f"Action loaded: {rec.name} ({path.name})")
                else:
                    logger(f"Action rejected: {path.name} — {rec.error}")

        except Exception as e:
            rec = ActionRecord(name=path.stem, file=path.name, error=f"Failed to load: {e}")
            traceback.print_exc()
            all_records.append(rec)
            logger(f"Action rejected: {path.name} — {rec.error}")

    registry = ActionRegistry(valid, logger)
    registry._all_records = all_records
    logger(f"Action discovery complete: {len(valid)} active.")
    return registry


_default_registry: Optional[ActionRegistry] = None


def get_default_registry(actions_dir: Path | None = None) -> ActionRegistry:
    """Return or create the authoritative shared ActionRegistry instance."""
    global _default_registry
    if _default_registry is None:
        p = actions_dir or (Path(__file__).resolve().parent.parent / "actions")
        _default_registry = discover_actions(p)
    return _default_registry


def set_default_registry(registry: ActionRegistry) -> None:
    """Set the shared singleton ActionRegistry."""
    global _default_registry
    _default_registry = registry


# Public alias for modules calling get_registry
get_registry = get_default_registry
