"""Unit coverage for scheduling and local SQLite persistence."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from progress_store import (
    ProgressBackend,
    ProgressStore,
    create_postgres_pool,
    schedule_review,
)


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


class SchedulingTests(unittest.TestCase):
    def test_rating_intervals_are_ordered(self) -> None:
        again = schedule_review("Again", now=NOW)
        hard = schedule_review("Hard", now=NOW)
        good = schedule_review("Good", now=NOW)
        easy = schedule_review("Easy", now=NOW)
        self.assertLess(again.next_due, hard.next_due)
        self.assertLessEqual(hard.next_due, good.next_due)
        self.assertLess(good.next_due, easy.next_due)

    def test_repeated_success_increases_interval(self) -> None:
        first = schedule_review("Good", now=NOW)
        second = schedule_review(
            "Good",
            review_count=first.review_count,
            interval_days=first.interval_days,
            ease_factor=first.ease_factor,
            now=NOW,
        )
        self.assertGreater(second.interval_days, first.interval_days)

    def test_again_reduces_ease_but_not_below_floor(self) -> None:
        result = schedule_review("Again", ease_factor=1.35, now=NOW)
        self.assertEqual(result.ease_factor, 1.3)


class ProgressStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nested" / "progress.db"
        self.store = ProgressStore(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_initialization_creates_empty_database(self) -> None:
        self.assertTrue(self.path.exists())
        self.assertEqual(self.store.table_count("card_progress"), 0)
        reopened = ProgressStore(self.path)
        self.assertEqual(reopened.table_count("quiz_attempts"), 0)

    def test_sqlite_connections_are_closed_after_use(self) -> None:
        with self.store._backend.connection() as connection:
            connection.execute("SELECT 1").fetchone()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_card_rating_and_bookmark_persist(self) -> None:
        self.store.rate_card("card", "Section 2", "deck", "Good", now=NOW)
        self.store.set_bookmark("card", "Section 2", "deck", True)
        record = self.store.get_card_progress(["card"])["card"]
        self.assertEqual(record["review_count"], 1)
        self.assertEqual(record["last_rating"], "Good")
        self.assertEqual(record["bookmarked"], 1)

    def test_deck_progress_is_filtered_and_user_scoped(self) -> None:
        learner_a = ProgressStore(self.path, user_id="learner-a")
        learner_b = ProgressStore(self.path, user_id="learner-b")
        learner_a.rate_card("qa-card", "Section 2", "flashcards_qa", "Good", now=NOW)
        learner_a.rate_card(
            "term-card",
            "Section 2",
            "flashcards_terms",
            "Good",
            now=NOW,
        )
        learner_b.rate_card("other-card", "Section 2", "flashcards_qa", "Good", now=NOW)
        self.assertEqual(
            set(learner_a.get_deck_progress("flashcards_qa")),
            {"qa-card"},
        )

    def test_mistake_history_and_resolution_persist(self) -> None:
        self.store.record_quiz_attempt(
            quiz_id="quiz",
            section="Section 2",
            topic="Topic",
            score=0,
            total=1,
            incorrect_ids=["question"],
            correct_ids=[],
            now=NOW,
        )
        self.assertEqual(len(self.store.unresolved_mistakes()), 1)
        self.store.record_mistake_review("question", False, now=NOW)
        mistake = self.store.unresolved_mistakes()[0]
        self.assertEqual(mistake["times_missed"], 2)
        self.store.record_mistake_review("question", True, now=NOW)
        self.assertEqual(self.store.unresolved_mistakes(), [])
        self.assertEqual(self.store.table_count("quiz_attempts"), 1)
        self.assertEqual(self.store.table_count("mistake_review_events"), 2)

    def test_later_correct_quiz_answer_resolves_mistake(self) -> None:
        common = dict(
            quiz_id="quiz",
            section="Section 2",
            topic="Topic",
            total=1,
            now=NOW,
        )
        self.store.record_quiz_attempt(
            **common, score=0, incorrect_ids=["question"], correct_ids=[]
        )
        self.store.record_quiz_attempt(
            **common, score=1, incorrect_ids=[], correct_ids=["question"]
        )
        self.assertEqual(self.store.unresolved_mistakes(), [])
        self.assertEqual(self.store.table_count("quiz_attempts"), 2)

    def test_reset_preserves_schema_and_clears_records(self) -> None:
        self.store.rate_card("card", "Section 2", "deck", "Good", now=NOW)
        self.store.reset_all_progress()
        self.assertEqual(self.store.table_count("card_progress"), 0)
        ProgressStore(self.path)

    def test_users_are_isolated_in_one_database(self) -> None:
        learner_a = ProgressStore(self.path, user_id="learner-a")
        learner_b = ProgressStore(self.path, user_id="learner-b")
        learner_a.rate_card("card", "Section 2", "deck", "Good", now=NOW)
        learner_a.record_quiz_attempt(
            quiz_id="quiz",
            section="Section 2",
            topic="Topic",
            score=0,
            total=1,
            incorrect_ids=["question"],
            correct_ids=[],
            now=NOW,
        )
        learner_a.save_active_session({"version": 1, "state": {"main_nav": "Quizzes"}}, "data")
        self.assertIn("card", learner_a.get_card_progress(["card"]))
        self.assertNotIn("card", learner_b.get_card_progress(["card"]))
        self.assertEqual(len(learner_a.unresolved_mistakes()), 1)
        self.assertEqual(learner_b.unresolved_mistakes(), [])
        self.assertIsNotNone(learner_a.load_active_session())
        self.assertIsNone(learner_b.load_active_session())

    def test_reset_only_deletes_current_user(self) -> None:
        learner_a = ProgressStore(self.path, user_id="learner-a")
        learner_b = ProgressStore(self.path, user_id="learner-b")
        learner_a.rate_card("card-a", "Section 2", "deck", "Good", now=NOW)
        learner_b.rate_card("card-b", "Section 3", "deck", "Good", now=NOW)
        learner_a.reset_all_progress()
        self.assertEqual(learner_a.table_count("card_progress"), 0)
        self.assertEqual(learner_b.table_count("card_progress"), 1)

    def test_active_session_round_trip(self) -> None:
        payload = {
            "version": 1,
            "state": {"main_nav": "Flashcards", "fc_index": 7},
        }
        self.store.save_active_session(payload, "fingerprint")
        saved = self.store.load_active_session()
        self.assertEqual(saved["payload"], payload)
        self.assertEqual(saved["data_fingerprint"], "fingerprint")
        self.store.clear_active_session()
        self.assertIsNone(self.store.load_active_session())

    def test_version_one_sqlite_data_migrates_to_local_user(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.db"
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(
                """
                CREATE TABLE schema_meta(version INTEGER NOT NULL);
                INSERT INTO schema_meta VALUES (1);
                CREATE TABLE card_progress(
                    card_id TEXT PRIMARY KEY, section TEXT, deck TEXT,
                    last_reviewed TEXT, next_due TEXT, review_count INTEGER,
                    last_rating TEXT, interval_days REAL, ease_factor REAL,
                    bookmarked INTEGER
                );
                INSERT INTO card_progress VALUES(
                    'legacy-card', 'Section 2', 'deck', NULL, NULL, 3,
                    'Good', 2, 2.5, 1
                );
                CREATE TABLE card_review_events(
                    id INTEGER PRIMARY KEY, card_id TEXT, section TEXT, deck TEXT,
                    rating TEXT, reviewed_at TEXT, next_due TEXT
                );
                CREATE TABLE quiz_attempts(
                    id INTEGER PRIMARY KEY, quiz_id TEXT, section TEXT, topic TEXT,
                    score INTEGER, total INTEGER, attempted_at TEXT,
                    incorrect_ids_json TEXT, mode TEXT
                );
                CREATE TABLE mistakes(
                    question_id TEXT PRIMARY KEY, quiz_id TEXT, section TEXT,
                    first_missed_at TEXT, last_missed_at TEXT, resolved_at TEXT,
                    times_missed INTEGER
                );
                CREATE TABLE mistake_review_events(
                    id INTEGER PRIMARY KEY, question_id TEXT, correct INTEGER,
                    reviewed_at TEXT
                );
                CREATE TABLE short_answer_reviews(
                    id INTEGER PRIMARY KEY, question_id TEXT, quiz_id TEXT,
                    section TEXT, rating TEXT, response TEXT, reviewed_at TEXT
                );
                """
            )
        migrated = ProgressStore(legacy_path)
        record = migrated.get_card_progress(["legacy-card"])["legacy-card"]
        self.assertEqual(record["review_count"], 3)
        self.assertEqual(record["bookmarked"], 1)

    def test_postgres_dialect_uses_postgres_identity_and_placeholders(self) -> None:
        postgres = ProgressBackend.__new__(ProgressBackend)
        postgres.kind = "postgres"
        schema = "\n".join(postgres.schema_statements())
        self.assertIn("GENERATED BY DEFAULT AS IDENTITY", schema)
        self.assertNotIn("AUTOINCREMENT", schema)
        self.assertEqual(
            postgres.sql("SELECT * FROM table_name WHERE a=? AND b=?"),
            "SELECT * FROM table_name WHERE a=%s AND b=%s",
        )

    def test_two_users_can_share_one_backend(self) -> None:
        backend = ProgressBackend(self.path)
        learner_a = ProgressStore(user_id="learner-a", backend=backend)
        learner_b = ProgressStore(user_id="learner-b", backend=backend)
        self.assertIs(learner_a._backend, learner_b._backend)
        with self.assertRaises(AttributeError):
            learner_a.user_id = "learner-b"
        learner_a.rate_card("card-a", "Section 2", "deck", "Good", now=NOW)
        self.assertIn("card-a", learner_a.get_card_progress(["card-a"]))
        self.assertNotIn("card-a", learner_b.get_card_progress(["card-a"]))

    def test_dashboard_uses_two_database_statements(self) -> None:
        backend = self.store._backend
        original_execute = backend.execute
        statement_count = 0

        def counted_execute(connection, query, params=()):
            nonlocal statement_count
            statement_count += 1
            return original_execute(connection, query, params)

        backend.execute = counted_execute
        try:
            self.store.dashboard_stats([])
        finally:
            backend.execute = original_execute
        self.assertEqual(statement_count, 2)

    def test_postgres_pool_is_bounded_and_waits_for_readiness(self) -> None:
        with patch("psycopg_pool.ConnectionPool") as pool_type:
            pool = pool_type.return_value
            created = create_postgres_pool("postgresql://example.invalid/database")

        self.assertIs(created, pool)
        options = pool_type.call_args.kwargs
        self.assertEqual(options["min_size"], 1)
        self.assertEqual(options["max_size"], 4)
        self.assertEqual(options["max_waiting"], 20)
        self.assertIsNone(options["kwargs"]["prepare_threshold"])
        pool.wait.assert_called_once_with(timeout=10)

    def test_owned_pool_closes_if_schema_initialization_fails(self) -> None:
        pool = MagicMock()
        with (
            patch("progress_store.create_postgres_pool", return_value=pool),
            patch.object(
                ProgressBackend,
                "_initialize",
                side_effect=RuntimeError("schema failed"),
            ),
            patch("progress_store.atexit.register") as register,
        ):
            with self.assertRaisesRegex(RuntimeError, "schema failed"):
                ProgressBackend(database_url="postgresql://example.invalid/database")

        pool.close.assert_called_once_with()
        register.assert_not_called()

    def test_owned_pool_registers_shutdown_after_schema_initialization(self) -> None:
        pool = MagicMock()
        with (
            patch("progress_store.create_postgres_pool", return_value=pool),
            patch.object(ProgressBackend, "_initialize"),
            patch("progress_store.atexit.register") as register,
        ):
            ProgressBackend(database_url="postgresql://example.invalid/database")

        pool.close.assert_not_called()
        register.assert_called_once_with(pool.close)

    def test_postgres_backend_checks_out_from_injected_pool(self) -> None:
        pool = MagicMock()
        connection = object()
        pool.connection.return_value.__enter__.return_value = connection
        postgres = ProgressBackend.__new__(ProgressBackend)
        postgres.kind = "postgres"
        postgres.pool = pool

        with postgres.connection() as checked_out:
            self.assertIs(checked_out, connection)

        pool.connection.assert_called_once_with(timeout=10)

    def test_postgres_batching_uses_cursor_api(self) -> None:
        postgres = ProgressBackend.__new__(ProgressBackend)
        postgres.kind = "postgres"
        connection = MagicMock(spec=["cursor"])
        cursor = connection.cursor.return_value

        postgres.executemany(
            connection,
            "INSERT INTO example(a, b) VALUES (?, ?)",
            [(1, 2), (3, 4)],
        )

        cursor.executemany.assert_called_once_with(
            "INSERT INTO example(a, b) VALUES (%s, %s)",
            [(1, 2), (3, 4)],
        )
        cursor.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
