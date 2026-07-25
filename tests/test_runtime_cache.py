"""Regression coverage for process-wide prepared study-data caching."""

from __future__ import annotations

import copy
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app
from tests.test_app import tiny_data


class RuntimeDataCacheTests(unittest.TestCase):
    def tearDown(self) -> None:
        app.load_data.clear()
        app.open_progress_backend.clear()

    def test_same_version_reuses_bundle_and_new_version_rebuilds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.json"
            source = tiny_data("Version one")
            path.write_text(json.dumps(source), encoding="utf-8")
            app.load_data.clear()

            first = app.load_data(str(path), 1)
            second = app.load_data(str(path), 1)
            self.assertIs(first, second)
            self.assertEqual(first.data, source)
            cached_snapshot = copy.deepcopy(first)
            app.filter_cards(
                first.cards,
                deck="flashcards_qa",
                query="literal",
            )
            app.weighted_mock_sample(first.questions, 1, random.Random(7))
            self.assertEqual(first, cached_snapshot)

            changed = tiny_data("Version two")
            path.write_text(json.dumps(changed), encoding="utf-8")
            rebuilt = app.load_data(str(path), 2)
            self.assertIsNot(rebuilt, first)
            self.assertEqual(rebuilt.cards[0]["front"], "Version two")
            self.assertEqual(first.data, source)

    def test_progress_backend_cache_is_shared_independently_of_user(self) -> None:
        app.open_progress_backend.clear()
        with patch.object(app, "ProgressBackend") as backend_type:
            backend_type.side_effect = [object(), object()]
            first = app.open_progress_backend(
                "/tmp/dea-progress.db",
                "postgresql://first.invalid/database",
            )
            second = app.open_progress_backend(
                "/tmp/dea-progress.db",
                "postgresql://first.invalid/database",
            )
            other_database = app.open_progress_backend(
                "/tmp/dea-progress.db",
                "postgresql://second.invalid/database",
            )

        self.assertIs(first, second)
        self.assertIsNot(first, other_database)
        self.assertEqual(backend_type.call_count, 2)

    def test_user_stores_share_cached_backend_in_production_composition(self) -> None:
        app.open_progress_backend.clear()
        backend = MagicMock()
        backend.path = Path("/tmp/dea-progress.db")
        backend.database_url = "postgresql://first.invalid/database"
        backend.kind = "postgres"
        with patch.object(app, "ProgressBackend", return_value=backend) as backend_type:
            learner_a = app.open_progress_store(
                "/tmp/dea-progress.db",
                "postgresql://first.invalid/database",
                "learner-a",
            )
            learner_b = app.open_progress_store(
                "/tmp/dea-progress.db",
                "postgresql://first.invalid/database",
                "learner-b",
            )

        backend_type.assert_called_once()
        self.assertIs(learner_a._backend, backend)
        self.assertIs(learner_b._backend, backend)
        self.assertEqual(learner_a.user_id, "learner-a")
        self.assertEqual(learner_b.user_id, "learner-b")


if __name__ == "__main__":
    unittest.main()
