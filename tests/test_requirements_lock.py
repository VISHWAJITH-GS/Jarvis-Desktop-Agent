"""
tests/test_requirements_lock.py — Unit Tests for requirements.lock Integrity
"""

import sys
import unittest
from pathlib import Path
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


class TestRequirementsLock(unittest.TestCase):
    def test_all_requirements_txt_packages_appear_in_lock(self):
        """Every package in requirements.txt (respecting platform markers) must appear in requirements.lock."""
        req_file = Path("requirements.txt")
        lock_file = Path("requirements.lock")

        self.assertTrue(req_file.exists(), "requirements.txt must exist")
        self.assertTrue(lock_file.exists(), "requirements.lock must exist")

        # Parse locked packages
        lock_content = lock_file.read_text(encoding="utf-8")
        locked_canonical_names = set()
        for line in lock_content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Parse package name before == or @
            pkg_name = line.split("==")[0].split("@")[0].strip()
            locked_canonical_names.add(canonicalize_name(pkg_name))

        # Parse requirements.txt
        req_content = req_file.read_text(encoding="utf-8")
        missing_from_lock = []
        checked_count = 0

        for line in req_content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            req_str = line.split("#")[0].strip()
            if not req_str:
                continue

            req = Requirement(req_str)
            # Evaluate platform marker against current sys.platform
            if req.marker is not None and not req.marker.evaluate({"sys_platform": sys.platform}):
                continue

            checked_count += 1
            canon_name = canonicalize_name(req.name)
            if canon_name not in locked_canonical_names:
                missing_from_lock.append(req.name)

        self.assertGreater(checked_count, 0, "Should have evaluated active requirements")
        self.assertEqual(
            missing_from_lock,
            [],
            f"The following required packages for {sys.platform} are missing from requirements.lock: {missing_from_lock}",
        )


if __name__ == "__main__":
    unittest.main()
