"""Unit coverage for durable working-session serialization."""

from __future__ import annotations

import unittest

from session_persistence import (
    capture_session_state,
    clear_working_session_state,
    restore_session_state,
)


class SessionPersistenceTests(unittest.TestCase):
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
        self.assertEqual(target["quiz_session"]["answers"], {0: "B"})

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


if __name__ == "__main__":
    unittest.main()
