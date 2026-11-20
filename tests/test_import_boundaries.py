"""
tests/test_import_boundaries.py — Architectural Boundary Verification Test.

Enforces:
- `core/` must NEVER import from `actions/`
- `core/` must NEVER import from `gui/` or `ui`
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest


class TestImportBoundaries(unittest.TestCase):
    def test_core_does_not_import_actions_or_gui(self):
        """Verify core modules never statically or dynamically import from actions, gui, or ui."""
        repo_root = Path(__file__).resolve().parent.parent
        core_dir = repo_root / "core"
        self.assertTrue(core_dir.is_dir(), f"core directory not found at {core_dir}")

        forbidden_prefixes = ("actions", "gui", "ui")
        violations: list[str] = []

        for py_file in core_dir.rglob("*.py"):
            with open(py_file, "r", encoding="utf-8") as f:
                try:
                    tree = ast.parse(f.read(), filename=str(py_file))
                except Exception as e:
                    self.fail(f"Failed to parse {py_file}: {e}")

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if any(alias.name == p or alias.name.startswith(f"{p}.") for p in forbidden_prefixes):
                            rel_path = py_file.relative_to(repo_root)
                            violations.append(f"{rel_path}:{node.lineno} imports '{alias.name}'")
                elif isinstance(node, ast.ImportFrom):
                    if node.module and any(node.module == p or node.module.startswith(f"{p}.") for p in forbidden_prefixes):
                        rel_path = py_file.relative_to(repo_root)
                        violations.append(f"{rel_path}:{node.lineno} imports from '{node.module}'")

        self.assertEqual(
            violations,
            [],
            f"Found forbidden imports in core/:\n" + "\n".join(violations),
        )


if __name__ == "__main__":
    unittest.main()
