"""
scripts/fix_future_imports.py — Ensure from __future__ import annotations is at the top of test files.
"""
import glob

def fix():
    for path in glob.glob("tests/test_*.py"):
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()

        lines = text.splitlines(keepends=True)
        fut_idx = -1
        for i, line in enumerate(lines):
            if line.startswith("from __future__ import annotations"):
                fut_idx = i
                break

        if fut_idx > 0:
            fut_line = lines.pop(fut_idx)
            # Find the first non-comment non-docstring location or after initial docstring
            insert_idx = 0
            if lines[0].strip().startswith('"""'):
                for j in range(len(lines)):
                    if j > 0 and '"""' in lines[j]:
                        insert_idx = j + 1
                        break
            lines.insert(insert_idx, fut_line)
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            print(f"Fixed future import in {path}")

if __name__ == "__main__":
    fix()
