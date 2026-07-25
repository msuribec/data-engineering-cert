"""Deployment dependency-manifest checks."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DependencyManifestTests(unittest.TestCase):
    def test_streamlit_auth_extra_is_enabled(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("streamlit[auth]", requirements.casefold())

    def test_psycopg_pool_extra_is_enabled(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("psycopg[binary,pool]", requirements.casefold())


if __name__ == "__main__":
    unittest.main()
