"""Streamlit AppTest coverage for important end-to-end flows."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]


def tiny_data(front: str = "Literal card") -> dict:
    cards = [
        {
            "front": front,
            "back": "Purpose: Keep aws/<service> & x > y safe. | When to use / notes: Always.",
            "tags": "DEA-C01 Section-2-Fundamentals Domain-1",
            "section": "Section 2 - Fundamentals",
        },
        {
            "front": "Second card",
            "back": "Second answer",
            "tags": "DEA-C01 Section-10-Security Domain-4",
            "section": "Section 10 - Security",
        },
    ]
    quiz = {
        "section": "Section 2 - Fundamentals",
        "topic": "Topic",
        "title": "Quiz — Topic",
        "domain": 1,
        "mc": [
            {
                "type": "mc",
                "question": "Is aws/<service> safe & literal?",
                "options": {"A": "<yes>", "B": "no & never"},
                "correct": "A",
                "explanation": "The <text> remains literal & inert.",
            },
            {
                "type": "mc",
                "question": "Second?",
                "options": {"A": "right", "B": "wrong"},
                "correct": "A",
                "explanation": "Explanation.",
            },
        ],
        "short": [
            {
                "type": "short",
                "question": "Explain <service>.",
                "answer": "A model & answer.",
            }
        ],
    }
    return {
        "flashcards_qa": cards,
        "flashcards_services": cards,
        "flashcards_terms": cards,
        "quizzes": [quiz],
        "sections": ["Section 2 - Fundamentals"],
    }


class AppFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)
        self.data_path = directory / "study.json"
        self.progress_path = directory / "progress.db"
        self.data_path.write_text(json.dumps(tiny_data()), encoding="utf-8")
        self.old_data = os.environ.get("DEA_STUDY_DATA_PATH")
        self.old_progress = os.environ.get("DEA_STUDY_PROGRESS_PATH")
        self.old_database = os.environ.get("DEA_STUDY_DATABASE_URL")
        os.environ["DEA_STUDY_DATA_PATH"] = str(self.data_path)
        os.environ["DEA_STUDY_PROGRESS_PATH"] = str(self.progress_path)
        os.environ.pop("DEA_STUDY_DATABASE_URL", None)

    def tearDown(self) -> None:
        if self.old_data is None:
            os.environ.pop("DEA_STUDY_DATA_PATH", None)
        else:
            os.environ["DEA_STUDY_DATA_PATH"] = self.old_data
        if self.old_progress is None:
            os.environ.pop("DEA_STUDY_PROGRESS_PATH", None)
        else:
            os.environ["DEA_STUDY_PROGRESS_PATH"] = self.old_progress
        if self.old_database is None:
            os.environ.pop("DEA_STUDY_DATABASE_URL", None)
        else:
            os.environ["DEA_STUDY_DATABASE_URL"] = self.old_database
        self.temp.cleanup()

    def app(self) -> AppTest:
        return AppTest.from_file(str(ROOT / "app.py"), default_timeout=30).run()

    def test_dashboard_and_empty_progress_database(self) -> None:
        at = self.app()
        self.assertEqual(at.exception, [])
        self.assertTrue(self.progress_path.exists())
        self.assertEqual(at.header[0].value, "Dashboard")
        self.assertIn("Your study history is empty", at.info[0].value)

    def test_flashcard_shuffle_persists_through_navigation(self) -> None:
        at = self.app()
        at.radio[0].set_value("Flashcards").run()
        next(button for button in at.button if button.label == "Shuffle queue").click().run()
        shuffled = list(at.session_state["fc_queue_ids"])
        first_id = shuffled[0]
        next(button for button in at.button if button.label == "Next").click().run()
        self.assertEqual(list(at.session_state["fc_queue_ids"]), shuffled)
        self.assertEqual(at.session_state["fc_index"], 1)
        next(button for button in at.button if button.label == "Previous").click().run()
        self.assertEqual(at.session_state["fc_index"], 0)
        self.assertEqual(at.session_state["fc_queue_ids"][0], first_id)

    def test_flashcard_clear_filters(self) -> None:
        at = self.app()
        at.radio[0].set_value("Flashcards").run()
        at.text_input[0].set_value("security").run()
        next(button for button in at.button if button.label == "Clear filters").click().run()
        self.assertEqual(at.exception, [])
        self.assertEqual(at.text_input[0].value, "")

    def test_html_like_card_content_is_not_raw_html(self) -> None:
        self.data_path.write_text(
            json.dumps(tiny_data("aws/<service> <script>alert(1)</script> & x > y")),
            encoding="utf-8",
        )
        at = self.app()
        at.radio[0].set_value("Flashcards").run()
        dynamic_markdown = [
            element.value for element in at.markdown if not element.value.startswith("<style>")
        ]
        rendered = "\n".join(dynamic_markdown)
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&lt;service&gt;", rendered)

    def test_quiz_submission_lock_retake_and_topic_isolation(self) -> None:
        at = self.app()
        at.radio[0].set_value("Quizzes").run()
        answer_radios = [
            radio for radio in at.radio if radio.label.startswith("Answer for question")
        ]
        self.assertEqual(len(answer_radios), 2)
        for radio in answer_radios:
            radio.set_value("A")
        at.run()
        submit = next(button for button in at.button if button.label == "Submit quiz")
        self.assertFalse(submit.disabled)
        submit.click().run()
        self.assertEqual(at.metric[0].value, "2 / 2")
        frozen = [
            radio
            for radio in at.radio
            if radio.label.startswith("Answer for question") and radio.disabled
        ]
        self.assertEqual(len(frozen), 2)
        next(button for button in at.button if button.label == "Retake quiz").click().run()
        fresh = [
            radio for radio in at.radio if radio.label.startswith("Answer for question")
        ]
        self.assertTrue(all(radio.value is None and not radio.disabled for radio in fresh))

    def test_mock_exam_submission_and_dashboard(self) -> None:
        expanded = tiny_data()
        base = expanded["quizzes"][0]
        expanded["quizzes"] = []
        expanded["sections"] = []
        for number in range(1, 6):
            quiz = json.loads(json.dumps(base))
            quiz["section"] = f"Section {number + 1} - Area {number}"
            quiz["topic"] = f"Topic {number}"
            quiz["title"] = f"Quiz {number}"
            quiz["domain"] = ((number - 1) % 4) + 1
            expanded["quizzes"].append(quiz)
            expanded["sections"].append(quiz["section"])
        self.data_path.write_text(json.dumps(expanded), encoding="utf-8")
        at = self.app()
        at.radio[0].set_value("Mock Exam").run()
        next(button for button in at.button if button.label == "Start mock exam").click().run()
        self.assertEqual(at.exception, [])
        self.assertGreaterEqual(
            len([r for r in at.radio if r.label.startswith("Mock exam answer")]), 5
        )
        markdown = "\n".join(element.value for element in at.markdown)
        self.assertNotIn("**Correct answer**", markdown)
        answer_radios = [
            radio for radio in at.radio if radio.label.startswith("Mock exam answer")
        ]
        for radio in answer_radios:
            radio.set_value("A")
        at.run()
        next(button for button in at.button if button.label == "Submit mock exam").click().run()
        self.assertEqual(at.exception, [])
        self.assertEqual(at.metric[0].label, "Mock-exam score")
        self.assertEqual(len(at.dataframe), 2)
        at.radio[0].set_value("Dashboard").run()
        self.assertEqual(at.exception, [])
        self.assertNotIn("Your study history is empty", [item.value for item in at.info])
        self.assertNotEqual(at.metric[2].value, "—")

    def test_malformed_data_shows_actionable_error(self) -> None:
        self.data_path.write_text('{"broken":', encoding="utf-8")
        at = self.app()
        self.assertEqual(at.exception, [])
        self.assertIn("malformed JSON", at.error[0].value)

    def test_quiz_draft_resumes_in_a_new_streamlit_session(self) -> None:
        first = self.app()
        first.radio[0].set_value("Quizzes").run()
        answer = next(
            radio for radio in first.radio if radio.label == "Answer for question 1"
        )
        answer.set_value("B").run()

        resumed = self.app()
        self.assertEqual(resumed.exception, [])
        self.assertEqual(resumed.radio[0].value, "Quizzes")
        restored_answer = next(
            radio for radio in resumed.radio if radio.label == "Answer for question 1"
        )
        self.assertEqual(restored_answer.value, "B")
        self.assertTrue(
            any("Continued your saved session" in item.value for item in resumed.info)
        )

    def test_start_fresh_clears_draft_but_keeps_progress(self) -> None:
        at = self.app()
        at.radio[0].set_value("Flashcards").run()
        next(button for button in at.button if button.label == "Reveal answer").click().run()
        next(button for button in at.button if button.label.startswith("Good")).click().run()
        at.radio[0].set_value("Quizzes").run()
        next(
            radio for radio in at.radio if radio.label == "Answer for question 1"
        ).set_value("B").run()

        next(button for button in at.button if button.label == "Start fresh").click().run()
        self.assertEqual(at.exception, [])
        self.assertEqual(at.radio[0].value, "Dashboard")
        self.assertEqual(at.metric[0].value, "1")
        self.assertTrue(
            any("Started a fresh working session" in item.value for item in at.info)
        )

        resumed = self.app()
        self.assertEqual(resumed.radio[0].value, "Dashboard")
        resumed.radio[0].set_value("Quizzes").run()
        answer = next(
            radio for radio in resumed.radio if radio.label == "Answer for question 1"
        )
        self.assertIsNone(answer.value)

    def test_cloud_database_without_authentication_is_blocked(self) -> None:
        os.environ["DEA_STUDY_DATABASE_URL"] = "postgresql://example.invalid/database"
        at = self.app()
        self.assertEqual(at.exception, [])
        self.assertTrue(
            any("without OIDC authentication" in item.value for item in at.error)
        )


if __name__ == "__main__":
    unittest.main()
