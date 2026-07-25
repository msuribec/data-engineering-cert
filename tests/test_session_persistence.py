"""Unit coverage for durable working-session serialization."""

from __future__ import annotations

import hashlib
import json
import unittest

from session_persistence import (
    SAVED_DIGEST_KEY,
    capture_session_state,
    clear_working_session_state,
    ensure_session_scope,
    mark_session_payload_saved,
    persist_session_if_changed,
    restore_session_state,
    session_payload_digest,
)


class SessionPersistenceTests(unittest.TestCase):
    def test_flashcard_queue_checkpoint_is_compact(self) -> None:
        queue = [
            hashlib.sha256(f"card-{index}".encode()).hexdigest()
            for index in range(2_563)
        ]
        payload = capture_session_state(
            {
                "main_nav": "Flashcards",
                "fc_queue_ids": queue,
                "fc_index": 123,
                "fc_show_answer": True,
            }
        )
        saved = payload["state"]
        self.assertNotIn("fc_queue_ids", saved)
        self.assertIn("fc_queue_order", saved)
        compact_size = len(json.dumps(payload, separators=(",", ":")))
        raw_size = len(json.dumps(queue, separators=(",", ":")))
        self.assertLess(compact_size, raw_size // 5)

    def test_capture_uses_stable_question_ids_not_source_content(self) -> None:
        state = {
            "main_nav": "Mock Exam",
            "mock_session": {
                "status": "in_progress",
                "questions": [
                    {"_id": "question-1", "question": "Secret source content"},
                    {"_id": "question-2", "question": "More source content"},
                ],
                "answers": {0: "A"},
                "choice_orders": {"question-1": ["B", "A"]},
            },
            "mock_answer_0": "A",
            "unrelated_internal_key": "do not save",
        }
        payload = capture_session_state(state)
        mock = payload["state"]["mock_session"]
        self.assertEqual(mock["question_ids"], ["question-1", "question-2"])
        self.assertNotIn("questions", mock)
        self.assertNotIn("unrelated_internal_key", payload["state"])

    def test_restore_rehydrates_mock_and_integer_answer_keys(self) -> None:
        payload = {
            "version": 1,
            "state": {
                "main_nav": "Mock Exam",
                "mock_session": {
                    "status": "submitted",
                    "question_ids": ["question-1"],
                    "answers": {"0": "A"},
                    "choice_orders": {"question-1": ["A", "B"]},
                    "started_at": 1_721_234_567.25,
                },
                "quiz_session": {"answers": {"0": "B"}},
            },
        }
        target: dict = {}
        restored = restore_session_state(
            target,
            payload,
            {"question-1": {"_id": "question-1", "question": "Restored"}},
        )
        self.assertTrue(restored)
        self.assertEqual(target["mock_session"]["questions"][0]["_id"], "question-1")
        self.assertEqual(target["mock_session"]["answers"], {0: "A"})
        self.assertEqual(target["mock_session"]["started_at"], 1_721_234_567.25)
        self.assertEqual(target["quiz_session"]["answers"], {0: "B"})
        self.assertEqual(
            session_payload_digest(payload, "data"),
            session_payload_digest(capture_session_state(target), "data"),
        )

    def test_missing_mock_question_safely_returns_to_setup(self) -> None:
        payload = {
            "version": 1,
            "state": {
                "mock_session": {
                    "status": "in_progress",
                    "question_ids": ["removed-question"],
                },
                "mock_answer_0": "A",
            },
        }
        target: dict = {}
        self.assertTrue(restore_session_state(target, payload, {}))
        self.assertEqual(target["mock_session"], {"status": "setup"})
        self.assertNotIn("mock_answer_0", target)

    def test_clear_removes_only_working_session_keys(self) -> None:
        state = {
            "main_nav": "Quizzes",
            "quiz_answer_0": "A",
            "_session_initialized": True,
            "keep_me": "value",
        }
        clear_working_session_state(state)
        self.assertEqual(state, {"keep_me": "value"})

    def test_digest_is_canonical_across_key_order_and_json_key_types(self) -> None:
        first = {
            "version": 1,
            "state": {
                "quiz_session": {"answers": {0: "A", 1: "B"}},
                "main_nav": "Quizzes",
            },
        }
        second = {
            "state": {
                "main_nav": "Quizzes",
                "quiz_session": {"answers": {"1": "B", "0": "A"}},
            },
            "version": 1,
        }
        self.assertEqual(
            session_payload_digest(first, "data"),
            session_payload_digest(second, "data"),
        )
        self.assertNotEqual(
            session_payload_digest(first, "data", "learner-a"),
            session_payload_digest(first, "data", "learner-b"),
        )

    def test_session_scope_change_clears_previous_learners_state(self) -> None:
        state = {
            "_session_scope": "learner-a",
            "_session_initialized": True,
            "_session_saved_digest": "digest",
            "main_nav": "Quizzes",
            "quiz_answer_0": "A",
            "unrelated": "keep",
        }
        self.assertTrue(ensure_session_scope(state, "learner-b"))
        self.assertEqual(
            state,
            {"_session_scope": "learner-b", "unrelated": "keep"},
        )
        self.assertFalse(ensure_session_scope(state, "learner-b"))

    def test_unchanged_and_transient_state_skip_session_write(self) -> None:
        class RecordingStore:
            def __init__(self) -> None:
                self.saved: list[dict] = []

            def save_active_session(self, payload: dict, data_fingerprint: str) -> None:
                self.saved.append({"payload": payload, "fingerprint": data_fingerprint})

        store = RecordingStore()
        state = {"main_nav": "Dashboard"}
        self.assertTrue(persist_session_if_changed(store, state, "data"))
        self.assertFalse(persist_session_if_changed(store, state, "data"))
        state["transient_message"] = "not part of a checkpoint"
        self.assertFalse(persist_session_if_changed(store, state, "data"))
        state["main_nav"] = "Flashcards"
        self.assertTrue(persist_session_if_changed(store, state, "data"))
        self.assertEqual(len(store.saved), 2)

    def test_failed_save_retries_and_does_not_mark_digest(self) -> None:
        class FailingOnceStore:
            def __init__(self) -> None:
                self.calls = 0

            def save_active_session(self, payload: dict, data_fingerprint: str) -> None:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("temporary failure")

        store = FailingOnceStore()
        state = {"main_nav": "Dashboard"}
        with self.assertRaises(RuntimeError):
            persist_session_if_changed(store, state, "data")
        self.assertNotIn(SAVED_DIGEST_KEY, state)
        self.assertTrue(persist_session_if_changed(store, state, "data"))
        self.assertEqual(store.calls, 2)

    def test_restored_payload_is_not_immediately_rewritten(self) -> None:
        class RecordingStore:
            def __init__(self) -> None:
                self.calls = 0

            def save_active_session(self, payload: dict, data_fingerprint: str) -> None:
                self.calls += 1

        payload = {"version": 1, "state": {"main_nav": "Dashboard"}}
        state = {"main_nav": "Dashboard"}
        mark_session_payload_saved(state, payload, "data")
        store = RecordingStore()
        self.assertFalse(persist_session_if_changed(store, state, "data"))
        self.assertEqual(store.calls, 0)


if __name__ == "__main__":
    unittest.main()
