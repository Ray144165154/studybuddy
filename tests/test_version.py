"""Release bookkeeping: one version number, one source of truth.

v0.9.0 shipped a macOS bundle whose ``Info.plist`` still said 0.8.5 because the
version was hard-coded in the PyInstaller spec. These checks fail on that class of
drift before a tag is cut.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import studybuddy

ROOT = Path(__file__).resolve().parent.parent
_VERSION_LINE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


class TestVersion(unittest.TestCase):
    def test_package_version_matches_pyproject(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text("utf-8")
        match = _VERSION_LINE.search(pyproject)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(studybuddy.__version__, match.group(1))

    def test_macos_bundle_version_is_derived_not_hardcoded(self) -> None:
        spec = (ROOT / "packaging" / "studybuddy-desktop.spec").read_text("utf-8")
        self.assertIn("BUNDLE_VERSION", spec)
        self.assertIsNone(
            re.search(r'CFBundleShortVersionString"\s*:\s*"\d', spec),
            "spec 里不要写死版本号，改从 pyproject.toml 读取",
        )


if __name__ == "__main__":
    unittest.main()
