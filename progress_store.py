"""User-scoped SQLite/Postgres persistence and spaced-repetition scheduling."""

from __future__ import annotations

import atexit
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
POSTGRES_SCHEMA_LOCK_ID = 7_260_125
RATINGS = ("Again", "Hard", "Good", "Easy")
TABLES = (
    "study_card_review_events",
    "study_card_progress",
    "study_quiz_attempts",
    "study_mistakes",
    "study_mistake_review_events",
    "study_short_answer_reviews",
    "study_active_sessions",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class Schedule:
    next_due: datetime
    interval_days: float
    ease_factor: float
    review_count: int


def schedule_review(
    rating: str,
    *,
    review_count: int = 0,
    interval_days: float = 0,
    ease_factor: float = 2.5,
    now: datetime | None = None,
) -> Schedule:
    """Return a deterministic SM-2-inspired schedule."""
    if rating not in RATINGS:
        raise ValueError(f"Unknown rating: {rating}")
    reviewed_at = now or utc_now()
    ease = max(1.3, ease_factor)
    if rating == "Again":
        interval = 0.0
        ease = max(1.3, ease - 0.20)
        due = reviewed_at + timedelta(minutes=10)
    elif rating == "Hard":
        interval = 1.0 if interval_days <= 0 else max(1.0, round(interval_days * 1.2, 2))
        ease = max(1.3, ease - 0.15)
        due = reviewed_at + timedelta(days=interval)
    elif rating == "Good":
        interval = 1.0 if interval_days <= 0 else max(1.0, round(interval_days * ease, 2))
        due = reviewed_at + timedelta(days=interval)
    else:
        interval = 4.0 if interval_days <= 0 else max(4.0, round(interval_days * ease * 1.3, 2))
        ease += 0.15
        due = reviewed_at + timedelta(days=interval)
    return Schedule(due, interval, ease, review_count + 1)


def create_postgres_pool(database_url: str) -> Any:
    """Create one bounded process-wide pool for the Supabase session pooler."""
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except ImportError as exc:
        raise RuntimeError(
            "Postgres pooling requires psycopg_pool. Run: pip install -r requirements.txt"
        ) from exc

    pool = ConnectionPool(
        conninfo=database_url,
        min_size=1,
        max_size=4,
        timeout=10,
        max_waiting=20,
        max_idle=300,
        max_lifetime=1_800,
        reconnect_timeout=30,
        kwargs={
            "row_factory": dict_row,
            "prepare_threshold": None,
            "connect_timeout": 10,
        },
        name="dea-study-progress",
        open=True,
    )
    try:
        pool.wait(timeout=10)
    except Exception:
        pool.close()
        raise
    return pool


class ProgressBackend:
    """Shared database resources and schema, independent of learner identity."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        database_url: str | None = None,
        connection_pool: Any | None = None,
    ) -> None:
        if not database_url and path is None:
            raise ValueError("A SQLite path or Postgres database URL is required")
        if connection_pool is not None and not database_url:
            raise ValueError("A connection pool requires a Postgres database URL")
        self.path = path
        self.database_url = database_url
        self.kind = "postgres" if database_url else "sqlite"
        self.pool = connection_pool
        self._owns_pool = self.kind == "postgres" and self.pool is None
        if self.kind == "postgres" and self.pool is None:
            self.pool = create_postgres_pool(str(database_url))
        if self.kind == "sqlite" and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._initialize()
        except Exception:
            if self._owns_pool:
                self.pool.close()
            raise
        if self._owns_pool:
            atexit.register(self.pool.close)

    @contextmanager
    def connection(self) -> Iterator[Any]:
        """Yield a transactional connection and always release its resources."""
        if self.kind == "sqlite":
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                with connection:
                    yield connection
            finally:
                connection.close()
            return
        with self.pool.connection(timeout=10) as connection:
            yield connection

    def sql(self, query: str) -> str:
        return query if self.kind == "sqlite" else query.replace("?", "%s")

    def execute(self, connection: Any, query: str, params: Sequence[Any] = ()) -> Any:
        return connection.execute(self.sql(query), params)

    def executemany(
        self,
        connection: Any,
        query: str,
        params: Sequence[Sequence[Any]],
    ) -> None:
        cursor = connection.cursor()
        try:
            cursor.executemany(self.sql(query), params)
        finally:
            cursor.close()

    def schema_statements(self) -> list[str]:
        identity = (
            "INTEGER PRIMARY KEY AUTOINCREMENT"
            if self.kind == "sqlite"
            else "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"
        )
        return [
            """
            CREATE TABLE IF NOT EXISTS study_schema_meta (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS study_card_progress (
                user_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                section TEXT NOT NULL,
                deck TEXT NOT NULL,
                last_reviewed TEXT,
                next_due TEXT,
                review_count INTEGER NOT NULL DEFAULT 0,
                last_rating TEXT,
                interval_days REAL NOT NULL DEFAULT 0,
                ease_factor REAL NOT NULL DEFAULT 2.5,
                bookmarked INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, card_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS study_card_review_events (
                id {identity},
                user_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                section TEXT NOT NULL,
                deck TEXT NOT NULL,
                rating TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                next_due TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS study_quiz_attempts (
                id {identity},
                user_id TEXT NOT NULL,
                quiz_id TEXT NOT NULL,
                section TEXT NOT NULL,
                topic TEXT NOT NULL,
                score INTEGER NOT NULL,
                total INTEGER NOT NULL,
                attempted_at TEXT NOT NULL,
                incorrect_ids_json TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'quiz'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS study_mistakes (
                user_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                quiz_id TEXT NOT NULL,
                section TEXT NOT NULL,
                first_missed_at TEXT NOT NULL,
                last_missed_at TEXT NOT NULL,
                resolved_at TEXT,
                times_missed INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (user_id, question_id)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS study_mistake_review_events (
                id {identity},
                user_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                correct INTEGER NOT NULL,
                reviewed_at TEXT NOT NULL
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS study_short_answer_reviews (
                id {identity},
                user_id TEXT NOT NULL,
                question_id TEXT NOT NULL,
                quiz_id TEXT NOT NULL,
                section TEXT NOT NULL,
                rating TEXT NOT NULL,
                response TEXT NOT NULL,
                reviewed_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS study_active_sessions (
                user_id TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                data_fingerprint TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS study_card_due_idx ON study_card_progress(user_id, next_due)",
            "CREATE INDEX IF NOT EXISTS study_card_event_user_idx ON study_card_review_events(user_id, reviewed_at)",
            "CREATE INDEX IF NOT EXISTS study_quiz_user_idx ON study_quiz_attempts(user_id, attempted_at)",
        ]

    def _legacy_table_exists(self, connection: Any, table: str) -> bool:
        if self.kind != "sqlite":
            return False
        row = self.execute(
            connection,
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    def _migrate_legacy_sqlite(self, connection: Any) -> None:
        if not self._legacy_table_exists(connection, "card_progress"):
            return
        copies = [
            """
            INSERT OR IGNORE INTO study_card_progress
            SELECT 'local', card_id, section, deck, last_reviewed, next_due, review_count,
                   last_rating, interval_days, ease_factor, bookmarked
            FROM card_progress
            """,
            """
            INSERT INTO study_card_review_events(
                user_id, card_id, section, deck, rating, reviewed_at, next_due
            )
            SELECT 'local', card_id, section, deck, rating, reviewed_at, next_due
            FROM card_review_events
            """,
            """
            INSERT INTO study_quiz_attempts(
                user_id, quiz_id, section, topic, score, total, attempted_at,
                incorrect_ids_json, mode
            )
            SELECT 'local', quiz_id, section, topic, score, total, attempted_at,
                   incorrect_ids_json, mode
            FROM quiz_attempts
            """,
            """
            INSERT OR IGNORE INTO study_mistakes
            SELECT 'local', question_id, quiz_id, section, first_missed_at,
                   last_missed_at, resolved_at, times_missed
            FROM mistakes
            """,
            """
            INSERT INTO study_mistake_review_events(user_id, question_id, correct, reviewed_at)
            SELECT 'local', question_id, correct, reviewed_at FROM mistake_review_events
            """,
            """
            INSERT INTO study_short_answer_reviews(
                user_id, question_id, quiz_id, section, rating, response, reviewed_at
            )
            SELECT 'local', question_id, quiz_id, section, rating, response, reviewed_at
            FROM short_answer_reviews
            """,
        ]
        for query in copies:
            connection.execute(query)

    def _initialize(self) -> None:
        with self.connection() as connection:
            if self.kind == "postgres":
                self.execute(
                    connection,
                    "SELECT pg_advisory_xact_lock(?)",
                    (POSTGRES_SCHEMA_LOCK_ID,),
                )
            for statement in self.schema_statements():
                self.execute(connection, statement)
            row = self.execute(
                connection, "SELECT version FROM study_schema_meta WHERE id=1"
            ).fetchone()
            if row is None:
                if self.kind == "sqlite":
                    self._migrate_legacy_sqlite(connection)
                self.execute(
                    connection,
                    """
                    INSERT INTO study_schema_meta(id, version) VALUES (1, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (SCHEMA_VERSION,),
                )
            elif int(row["version"]) > SCHEMA_VERSION:
                raise RuntimeError("The progress database was created by a newer app version.")
            elif int(row["version"]) < SCHEMA_VERSION:
                self.execute(
                    connection,
                    "UPDATE study_schema_meta SET version=? WHERE id=1",
                    (SCHEMA_VERSION,),
                )


class ProgressStore:
    """A lightweight, immutable learner-scoped facade over shared storage."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        user_id: str = "local",
        database_url: str | None = None,
        backend: ProgressBackend | None = None,
        connection_pool: Any | None = None,
    ) -> None:
        if not user_id:
            raise ValueError("user_id is required")
        if backend is not None and (
            path is not None or database_url is not None or connection_pool is not None
        ):
            raise ValueError("Pass either a shared backend or connection settings, not both")
        self._backend = backend or ProgressBackend(
            path,
            database_url=database_url,
            connection_pool=connection_pool,
        )
        self.path = self._backend.path
        self.database_url = self._backend.database_url
        self.backend = self._backend.kind
        self._user_id = user_id

    @property
    def user_id(self) -> str:
        """Opaque learner identifier fixed for the lifetime of this facade."""
        return self._user_id

    def _connect(self) -> Any:
        return self._backend.connection()

    def _sql(self, query: str) -> str:
        return self._backend.sql(query)

    def _execute(self, connection: Any, query: str, params: Sequence[Any] = ()) -> Any:
        return self._backend.execute(connection, query, params)

    def _executemany(
        self,
        connection: Any,
        query: str,
        params: Sequence[Sequence[Any]],
    ) -> Any:
        return self._backend.executemany(connection, query, params)

    def _schema_statements(self) -> list[str]:
        """Compatibility helper for schema-dialect tests and diagnostics."""
        return self._backend.schema_statements()

    def get_card_progress(self, card_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not card_ids:
            return {}
        records: dict[str, dict[str, Any]] = {}
        batch_size = 5_000 if self.backend == "postgres" else 800
        with self._connect() as connection:
            for offset in range(0, len(card_ids), batch_size):
                batch = list(card_ids[offset : offset + batch_size])
                marks = ",".join("?" for _ in batch)
                rows = self._execute(
                    connection,
                    f"""
                    SELECT * FROM study_card_progress
                    WHERE user_id=? AND card_id IN ({marks})
                    """,
                    [self.user_id, *batch],
                ).fetchall()
                records.update({row["card_id"]: dict(row) for row in rows})
        return records

    def rate_card(
        self,
        card_id: str,
        section: str,
        deck: str,
        rating: str,
        *,
        now: datetime | None = None,
    ) -> Schedule:
        reviewed_at = now or utc_now()
        with self._connect() as connection:
            previous = self._execute(
                connection,
                "SELECT * FROM study_card_progress WHERE user_id=? AND card_id=?",
                (self.user_id, card_id),
            ).fetchone()
            schedule = schedule_review(
                rating,
                review_count=previous["review_count"] if previous else 0,
                interval_days=previous["interval_days"] if previous else 0,
                ease_factor=previous["ease_factor"] if previous else 2.5,
                now=reviewed_at,
            )
            bookmarked = previous["bookmarked"] if previous else 0
            self._execute(
                connection,
                """
                INSERT INTO study_card_progress(
                    user_id, card_id, section, deck, last_reviewed, next_due,
                    review_count, last_rating, interval_days, ease_factor, bookmarked
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, card_id) DO UPDATE SET
                    section=excluded.section, deck=excluded.deck,
                    last_reviewed=excluded.last_reviewed, next_due=excluded.next_due,
                    review_count=excluded.review_count, last_rating=excluded.last_rating,
                    interval_days=excluded.interval_days, ease_factor=excluded.ease_factor
                """,
                (
                    self.user_id, card_id, section, deck, to_iso(reviewed_at),
                    to_iso(schedule.next_due), schedule.review_count, rating,
                    schedule.interval_days, schedule.ease_factor, bookmarked,
                ),
            )
            self._execute(
                connection,
                """
                INSERT INTO study_card_review_events(
                    user_id, card_id, section, deck, rating, reviewed_at, next_due
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.user_id, card_id, section, deck, rating,
                    to_iso(reviewed_at), to_iso(schedule.next_due),
                ),
            )
        return schedule

    def set_bookmark(self, card_id: str, section: str, deck: str, bookmarked: bool) -> None:
        with self._connect() as connection:
            self._execute(
                connection,
                """
                INSERT INTO study_card_progress(user_id, card_id, section, deck, bookmarked)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, card_id) DO UPDATE SET bookmarked=excluded.bookmarked
                """,
                (self.user_id, card_id, section, deck, int(bookmarked)),
            )

    def record_quiz_attempt(
        self,
        *,
        quiz_id: str,
        section: str,
        topic: str,
        score: int,
        total: int,
        incorrect_ids: Sequence[str],
        correct_ids: Sequence[str],
        mode: str = "quiz",
        now: datetime | None = None,
        question_sources: Mapping[str, tuple[str, str]] | None = None,
    ) -> int:
        attempted_at = now or utc_now()
        sources = question_sources or {}
        with self._connect() as connection:
            cursor = self._execute(
                connection,
                """
                INSERT INTO study_quiz_attempts(
                    user_id, quiz_id, section, topic, score, total, attempted_at,
                    incorrect_ids_json, mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id
                """,
                (
                    self.user_id, quiz_id, section, topic, score, total,
                    to_iso(attempted_at), json.dumps(list(incorrect_ids)), mode,
                ),
            )
            attempt_id = int(cursor.fetchone()["id"])
            if incorrect_ids:
                mistake_rows = []
                for question_id in incorrect_ids:
                    source_quiz_id, source_section = sources.get(
                        question_id, (quiz_id, section)
                    )
                    mistake_rows.append(
                        (
                            self.user_id,
                            question_id,
                            source_quiz_id,
                            source_section,
                            to_iso(attempted_at),
                            to_iso(attempted_at),
                        )
                    )
                self._executemany(
                    connection,
                    """
                    INSERT INTO study_mistakes(
                        user_id, question_id, quiz_id, section, first_missed_at,
                        last_missed_at, resolved_at, times_missed
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, 1)
                    ON CONFLICT(user_id, question_id) DO UPDATE SET
                        quiz_id=excluded.quiz_id, section=excluded.section,
                        last_missed_at=excluded.last_missed_at, resolved_at=NULL,
                        times_missed=study_mistakes.times_missed + 1
                    """,
                    mistake_rows,
                )
            if correct_ids:
                marks = ",".join("?" for _ in correct_ids)
                self._execute(
                    connection,
                    f"""
                    UPDATE study_mistakes SET resolved_at=?
                    WHERE user_id=? AND question_id IN ({marks}) AND resolved_at IS NULL
                    """,
                    (to_iso(attempted_at), self.user_id, *correct_ids),
                )
            return attempt_id

    def unresolved_mistakes(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = self._execute(
                connection,
                """
                SELECT * FROM study_mistakes
                WHERE user_id=? AND resolved_at IS NULL ORDER BY last_missed_at DESC
                """,
                (self.user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_mistake_review(
        self, question_id: str, correct: bool, *, now: datetime | None = None
    ) -> None:
        reviewed_at = now or utc_now()
        with self._connect() as connection:
            self._execute(
                connection,
                """
                INSERT INTO study_mistake_review_events(
                    user_id, question_id, correct, reviewed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (self.user_id, question_id, int(correct), to_iso(reviewed_at)),
            )
            if correct:
                self._execute(
                    connection,
                    "UPDATE study_mistakes SET resolved_at=? WHERE user_id=? AND question_id=?",
                    (to_iso(reviewed_at), self.user_id, question_id),
                )
            else:
                self._execute(
                    connection,
                    """
                    UPDATE study_mistakes
                    SET last_missed_at=?, times_missed=times_missed + 1
                    WHERE user_id=? AND question_id=?
                    """,
                    (to_iso(reviewed_at), self.user_id, question_id),
                )

    def record_short_answer(
        self,
        *,
        question_id: str,
        quiz_id: str,
        section: str,
        rating: str,
        response: str,
        now: datetime | None = None,
    ) -> None:
        if rating not in ("Needs review", "Understood"):
            raise ValueError(f"Unknown self-rating: {rating}")
        with self._connect() as connection:
            self._execute(
                connection,
                """
                INSERT INTO study_short_answer_reviews(
                    user_id, question_id, quiz_id, section, rating, response, reviewed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.user_id, question_id, quiz_id, section, rating,
                    response, to_iso(now or utc_now()),
                ),
            )

    def dashboard_stats(
        self, all_card_ids: Sequence[str], *, now: datetime | None = None
    ) -> dict[str, Any]:
        current = now or utc_now()
        with self._connect() as connection:
            summary = self._execute(
                connection,
                """
                WITH section_stats AS (
                    SELECT section,
                           AVG(100.0 * score / NULLIF(total, 0)) AS average,
                           COUNT(*) AS attempts
                    FROM study_quiz_attempts
                    WHERE user_id=? AND total > 0 AND mode='quiz'
                    GROUP BY section
                ),
                recent AS (
                    SELECT id, quiz_id, section, topic, score, total, attempted_at,
                           incorrect_ids_json, mode
                    FROM study_quiz_attempts
                    WHERE user_id=?
                    ORDER BY attempted_at DESC
                    LIMIT 1
                )
                SELECT
                    (
                        SELECT COUNT(*) FROM study_card_progress
                        WHERE user_id=? AND review_count > 0
                    ) AS total_reviewed,
                    (
                        SELECT COUNT(*) FROM study_card_progress
                        WHERE user_id=? AND review_count > 0 AND next_due <= ?
                    ) AS due,
                    (
                        SELECT AVG(100.0 * score / NULLIF(total, 0))
                        FROM study_quiz_attempts
                        WHERE user_id=? AND total > 0
                    ) AS quiz_average,
                    (SELECT id FROM recent) AS recent_id,
                    (SELECT quiz_id FROM recent) AS recent_quiz_id,
                    (SELECT section FROM recent) AS recent_section,
                    (SELECT topic FROM recent) AS recent_topic,
                    (SELECT score FROM recent) AS recent_score,
                    (SELECT total FROM recent) AS recent_total,
                    (SELECT attempted_at FROM recent) AS recent_attempted_at,
                    (SELECT incorrect_ids_json FROM recent) AS recent_incorrect_ids_json,
                    (SELECT mode FROM recent) AS recent_mode,
                    (
                        SELECT section FROM section_stats
                        ORDER BY average DESC, section ASC LIMIT 1
                    ) AS strongest_section,
                    (
                        SELECT average FROM section_stats
                        ORDER BY average DESC, section ASC LIMIT 1
                    ) AS strongest_average,
                    (
                        SELECT attempts FROM section_stats
                        ORDER BY average DESC, section ASC LIMIT 1
                    ) AS strongest_attempts,
                    (
                        SELECT section FROM section_stats
                        ORDER BY average ASC, section ASC LIMIT 1
                    ) AS weakest_section,
                    (
                        SELECT average FROM section_stats
                        ORDER BY average ASC, section ASC LIMIT 1
                    ) AS weakest_average,
                    (
                        SELECT attempts FROM section_stats
                        ORDER BY average ASC, section ASC LIMIT 1
                    ) AS weakest_attempts
                """,
                (
                    self.user_id,
                    self.user_id,
                    self.user_id,
                    self.user_id,
                    to_iso(current),
                    self.user_id,
                ),
            ).fetchone()
            activity_rows = self._execute(
                connection,
                """
                SELECT happened_at, kind, section, detail
                FROM (
                    SELECT reviewed_at AS happened_at, 'card' AS kind,
                           section, rating AS detail
                    FROM study_card_review_events
                    WHERE user_id=?
                    UNION ALL
                    SELECT attempted_at AS happened_at, 'quiz' AS kind, section,
                           topic || ': ' || CAST(score AS TEXT) || '/' ||
                           CAST(total AS TEXT) AS detail
                    FROM study_quiz_attempts
                    WHERE user_id=?
                ) AS activity
                ORDER BY happened_at DESC
                LIMIT 8
                """,
                (self.user_id, self.user_id),
            ).fetchall()
        total_reviewed = int(summary["total_reviewed"])
        recent_quiz = None
        if summary["recent_id"] is not None:
            recent_quiz = {
                "id": summary["recent_id"],
                "user_id": self.user_id,
                "quiz_id": summary["recent_quiz_id"],
                "section": summary["recent_section"],
                "topic": summary["recent_topic"],
                "score": summary["recent_score"],
                "total": summary["recent_total"],
                "attempted_at": summary["recent_attempted_at"],
                "incorrect_ids_json": summary["recent_incorrect_ids_json"],
                "mode": summary["recent_mode"],
            }
        strongest = None
        if summary["strongest_section"] is not None:
            strongest = {
                "section": summary["strongest_section"],
                "average": summary["strongest_average"],
                "attempts": summary["strongest_attempts"],
            }
        weakest = None
        if summary["weakest_section"] is not None:
            weakest = {
                "section": summary["weakest_section"],
                "average": summary["weakest_average"],
                "attempts": summary["weakest_attempts"],
            }
        average = summary["quiz_average"]
        return {
            "total_reviewed": total_reviewed,
            "new_cards": max(0, len(all_card_ids) - total_reviewed),
            "due": int(summary["due"]),
            "quiz_average": float(average) if average is not None else None,
            "recent_quiz": recent_quiz,
            "strongest": strongest,
            "weakest": weakest,
            "recent_activity": [dict(row) for row in activity_rows],
            "has_activity": bool(total_reviewed or recent_quiz),
        }

    def save_active_session(
        self, payload: Mapping[str, Any], data_fingerprint: str
    ) -> None:
        with self._connect() as connection:
            self._execute(
                connection,
                """
                INSERT INTO study_active_sessions(
                    user_id, payload_json, data_fingerprint, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    data_fingerprint=excluded.data_fingerprint,
                    updated_at=excluded.updated_at
                """,
                (
                    self.user_id,
                    json.dumps(payload, ensure_ascii=False),
                    data_fingerprint,
                    to_iso(utc_now()),
                ),
            )

    def load_active_session(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = self._execute(
                connection,
                "SELECT * FROM study_active_sessions WHERE user_id=?",
                (self.user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "payload": json.loads(row["payload_json"]),
            "data_fingerprint": row["data_fingerprint"],
            "updated_at": row["updated_at"],
        }

    def clear_active_session(self) -> None:
        with self._connect() as connection:
            self._execute(
                connection,
                "DELETE FROM study_active_sessions WHERE user_id=?",
                (self.user_id,),
            )

    def reset_all_progress(self) -> None:
        """Delete only the current user's progress and session."""
        with self._connect() as connection:
            for table in TABLES:
                self._execute(
                    connection,
                    f"DELETE FROM {table} WHERE user_id=?",
                    (self.user_id,),
                )

    def table_count(self, table: str, *, all_users: bool = False) -> int:
        aliases = {
            "card_progress": "study_card_progress",
            "card_review_events": "study_card_review_events",
            "quiz_attempts": "study_quiz_attempts",
            "mistakes": "study_mistakes",
            "mistake_review_events": "study_mistake_review_events",
            "short_answer_reviews": "study_short_answer_reviews",
            "active_sessions": "study_active_sessions",
        }
        resolved = aliases.get(table)
        if resolved is None:
            raise ValueError("Unknown table")
        query = f"SELECT COUNT(*) AS count FROM {resolved}"
        params: tuple[Any, ...] = ()
        if not all_users:
            query += " WHERE user_id=?"
            params = (self.user_id,)
        with self._connect() as connection:
            return int(self._execute(connection, query, params).fetchone()["count"])
