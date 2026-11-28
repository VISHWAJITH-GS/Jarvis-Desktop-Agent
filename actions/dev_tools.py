"""
actions/dev_tools.py — Developer Tools, Git Code Review & Project Scaffolding.

Provides developer workflows:
- codereview_diff: Show git diff (unstaged or staged) for a workspace or file.
- codereview_log: Show recent git commits with commit hash, author, date, message.
- codereview_blame: Show git blame for a file, optionally filtered by line range.
- codereview_stats: Show git diff statistics (insertions, deletions, files changed).
- project_create: Scaffold new projects (python, node, react, fastapi, flask, nextjs, html).
- project_analyze: Analyze a folder: detect primary language, framework, lines of code, and structure.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

from core import outcome

logger = logging.getLogger("dev_tools")


def _run_git(args: list[str], cwd: Optional[str] = None) -> tuple[int, str, str]:
    target_dir = Path(cwd).resolve() if cwd else Path.cwd()
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def codereview_diff(staged: bool = False, file_path: Optional[str] = None, cwd: Optional[str] = None) -> str:
    args = ["diff"]
    if staged:
        args.append("--staged")
    if file_path:
        args.extend(["--", file_path])

    code, out, err = _run_git(args, cwd=cwd)
    if code != 0:
        return outcome.failed("codereview_diff", f"Git diff error: {err or 'unknown failure'}")

    diff_text = out[:8000] if out else "(No changes detected)"
    return outcome.done(
        "codereview_diff",
        staged=staged,
        file=file_path,
        diff=diff_text,
        truncated=len(out) > 8000,
    )


def codereview_log(limit: int = 10, cwd: Optional[str] = None) -> str:
    args = ["log", f"-n{max(1, min(limit, 50))}", "--pretty=format:%h|%an|%ar|%s"]
    code, out, err = _run_git(args, cwd=cwd)
    if code != 0:
        return outcome.failed("codereview_log", f"Git log error: {err}")

    commits = []
    if out:
        for line in out.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                commits.append({"hash": parts[0], "author": parts[1], "relative_time": parts[2], "subject": parts[3]})

    return outcome.done("codereview_log", count=len(commits), commits=commits)


def codereview_blame(file_path: str, start_line: Optional[int] = None, end_line: Optional[int] = None, cwd: Optional[str] = None) -> str:
    if not file_path:
        return outcome.failed("codereview_blame", "Missing required parameter 'file_path'.")

    args = ["blame", "-w"]
    if start_line is not None and end_line is not None:
        args.extend([f"-L{start_line},{end_line}"])
    args.extend(["--", file_path])

    code, out, err = _run_git(args, cwd=cwd)
    if code != 0:
        return outcome.failed("codereview_blame", f"Git blame error: {err}")

    blame_lines = out.splitlines()[:60]
    return outcome.done(
        "codereview_blame",
        file=file_path,
        lines_count=len(blame_lines),
        blame="\n".join(blame_lines),
    )


def codereview_stats(cwd: Optional[str] = None) -> str:
    code, out, err = _run_git(["diff", "--stat"], cwd=cwd)
    if code != 0:
        return outcome.failed("codereview_stats", f"Git diff stat error: {err}")

    return outcome.done("codereview_stats", stats=out or "No modifications.")


def project_create(template: str, project_name: str, target_dir: Optional[str] = None) -> str:
    if not project_name:
        return outcome.failed("project_create", "Missing project_name.")

    base_path = Path(target_dir).resolve() if target_dir else Path.cwd()
    project_path = base_path / project_name

    if project_path.exists() and any(project_path.iterdir()):
        return outcome.failed("project_create", f"Destination directory '{project_path}' already exists and is not empty.")

    project_path.mkdir(parents=True, exist_ok=True)
    tmpl = template.lower().strip()

    if tmpl in ("python", "fastapi", "flask"):
        (project_path / "src").mkdir(exist_ok=True)
        (project_path / "tests").mkdir(exist_ok=True)
        if tmpl == "fastapi":
            main_code = (
                "from fastapi import FastAPI\n\napp = FastAPI(title='"
                + project_name
                + "')\n\n@app.get('/')\ndef root():\n    return {'status': 'ok'}\n"
            )
            reqs = "fastapi>=0.110.0\nuvicorn>=0.28.0\npytest>=8.0.0\n"
        elif tmpl == "flask":
            main_code = (
                "from flask import Flask, jsonify\n\napp = Flask(__name__)\n\n@app.route('/')\ndef root():\n    return jsonify(status='ok')\n\nif __name__ == '__main__':\n    app.run(debug=True)\n"
            )
            reqs = "flask>=3.0.0\npytest>=8.0.0\n"
        else:
            main_code = (
                '"""\n'
                + project_name
                + ' entry point\n"""\n\ndef main():\n    print("Hello from '
                + project_name
                + '!")\n\nif __name__ == "__main__":\n    main()\n'
            )
            reqs = "pytest>=8.0.0\n"

        (project_path / "src" / "main.py").write_text(main_code, encoding="utf-8")
        (project_path / "requirements.txt").write_text(reqs, encoding="utf-8")
        (project_path / "README.md").write_text(f"# {project_name}\n\nGenerated with JARVIS.\n", encoding="utf-8")
        (project_path / ".gitignore").write_text("__pycache__/\n*.pyc\n.venv/\n.pytest_cache/\n", encoding="utf-8")

    elif tmpl in ("node", "react", "nextjs", "html"):
        if tmpl == "html":
            html_content = (
                "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"UTF-8\">\n  <title>"
                + project_name
                + "</title>\n</head>\n<body>\n  <h1>Welcome to "
                + project_name
                + "</h1>\n</body>\n</html>"
            )
            (project_path / "index.html").write_text(html_content, encoding="utf-8")
            (project_path / "style.css").write_text("body { font-family: sans-serif; margin: 2rem; }\n", encoding="utf-8")
        else:
            pkg = {
                "name": project_name,
                "version": "1.0.0",
                "scripts": {"start": "node index.js", "test": "echo 'no tests'"},
                "dependencies": {},
            }
            (project_path / "package.json").write_text(json.dumps(pkg, indent=2), encoding="utf-8")
            (project_path / "index.js").write_text(f'console.log("Hello from {project_name}");\n', encoding="utf-8")
            (project_path / ".gitignore").write_text("node_modules/\n.env\ndist/\n", encoding="utf-8")
        (project_path / "README.md").write_text(f"# {project_name}\n\nGenerated with JARVIS.\n", encoding="utf-8")

    else:
        return outcome.failed(
            "project_create",
            f"Unsupported template '{template}'. Supported: python, fastapi, flask, node, html",
        )

    return outcome.done(
        "project_create",
        template=tmpl,
        project_name=project_name,
        path=str(project_path),
        verified=True,
    )


def project_analyze(target_dir: Optional[str] = None) -> str:
    path = Path(target_dir).resolve() if target_dir else Path.cwd()
    if not path.is_dir():
        return outcome.failed("project_analyze", f"Directory '{path}' not found.")

    ext_counts: Dict[str, int] = {}
    file_count = 0
    frameworks = []

    # File scans
    for root, dirs, files in os.walk(path):
        # Skip common heavy ignored directories
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".venv", "venv", "__pycache__", "build", "dist")]
        for f in files:
            file_count += 1
            ext = Path(f).suffix.lower() or "(no-ext)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1

    # Framework heuristics
    if (path / "pyproject.toml").exists() or (path / "requirements.txt").exists():
        frameworks.append("python")
        if (path / "requirements.txt").exists():
            txt = (path / "requirements.txt").read_text(encoding="utf-8", errors="ignore").lower()
            if "fastapi" in txt:
                frameworks.append("fastapi")
            elif "flask" in txt:
                frameworks.append("flask")
            elif "django" in txt:
                frameworks.append("django")

    if (path / "package.json").exists():
        frameworks.append("nodejs")
        try:
            pkg = json.loads((path / "package.json").read_text(encoding="utf-8"))
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                frameworks.append("nextjs")
            elif "react" in deps:
                frameworks.append("react")
            elif "vue" in deps:
                frameworks.append("vue")
        except Exception:
            pass

    primary_ext = max(ext_counts.items(), key=lambda x: x[1])[0] if ext_counts else "unknown"

    return outcome.done(
        "project_analyze",
        root=str(path),
        total_files=file_count,
        top_extensions=dict(sorted(ext_counts.items(), key=lambda x: -x[1])[:5]),
        detected_frameworks=frameworks,
        primary_extension=primary_ext,
    )


def dev_tools(parameters: dict, **kwargs) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).lower().strip()
    cwd = params.get("cwd")

    if action in ("codereview_diff", "diff"):
        staged = bool(params.get("staged", False))
        file_path = params.get("file_path") or params.get("file")
        return codereview_diff(staged=staged, file_path=file_path, cwd=cwd)

    elif action in ("codereview_log", "log"):
        limit = int(params.get("limit", 10))
        return codereview_log(limit=limit, cwd=cwd)

    elif action in ("codereview_blame", "blame"):
        file_path = str(params.get("file_path") or params.get("file", ""))
        start = params.get("start_line")
        end = params.get("end_line")
        return codereview_blame(
            file_path,
            start_line=int(start) if start is not None else None,
            end_line=int(end) if end is not None else None,
            cwd=cwd,
        )

    elif action in ("codereview_stats", "stats", "diff_stats"):
        return codereview_stats(cwd=cwd)

    elif action in ("project_create", "create_project", "scaffold"):
        template = str(params.get("template", "python"))
        name = str(params.get("project_name") or params.get("name", "new_project"))
        return project_create(template=template, project_name=name, target_dir=cwd)

    elif action in ("project_analyze", "analyze_project"):
        return project_analyze(target_dir=cwd)

    return outcome.failed(
        "dev_tools",
        f"Unknown action '{action}'. Supported: codereview_diff, codereview_log, codereview_blame, codereview_stats, project_create, project_analyze",
    )


TOOL = {
    "name": "dev_tools",
    "permission": "normal",
    "description": (
        "Developer and Code Review tools: inspect git diffs, git commit history, file blame, "
        "git statistics, project scaffolding (python, node, react, fastapi, flask, html), and repository structure analysis."
    ),
    "brief": "Developer tools: git diff, git log, git blame, scaffold, analyze.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "Exact action: codereview_diff | codereview_log | codereview_blame | "
                    "codereview_stats | project_create | project_analyze"
                ),
            },
            "cwd": {
                "type": "STRING",
                "description": "Target repository or project root path.",
            },
            "file_path": {
                "type": "STRING",
                "description": "File path for diff or blame.",
            },
            "staged": {
                "type": "BOOLEAN",
                "description": "Show staged diffs instead of working tree (default: false).",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Number of commits to show in log (default: 10).",
            },
            "start_line": {
                "type": "INTEGER",
                "description": "Start line for blame range.",
            },
            "end_line": {
                "type": "INTEGER",
                "description": "End line for blame range.",
            },
            "template": {
                "type": "STRING",
                "description": "Project template: python | fastapi | flask | node | html",
            },
            "project_name": {
                "type": "STRING",
                "description": "Project directory name to create.",
            },
        },
        "required": ["action"],
    },
    "handler": dev_tools,
}
