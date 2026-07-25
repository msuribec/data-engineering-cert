"""Unit coverage for identities, validation, filtering, queues, and quiz state."""

from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from pathlib import Path

from study_core import (
    StudyDataError,
    canonical_section_key,
    data_file_version,
    ensure_review_queue,
    escape_markdown_text,
    filter_cards,
    load_study_data,
    make_filter_signature,
    prepare_quiz_state,
    retake_quiz_state,
    section_sort_key,
    stable_card_id,
    submit_quiz_state,
    validate_study_data,
    weighted_mock_sample,
)


def valid_data() -> dict:
    return {
        "flashcards_qa": [
            {
                "front": "Question",
                "back": "Answer",
                "tags": "DEA-C01 Section-2-Fundamentals Domain-1",
                "section": "Section 2 - Fundamentals",
            }
        ],
        "flashcards_services": [],
        "flashcards_terms": [],
        "quizzes": [
            {
                "section": "Section 2 - Fundamentals",
                "topic": "Topic",
                "title": "Quiz — Topic",
                "domain": 1,
                "mc": [
                    {
                        "type": "mc",
                        "question": "Question?",
                        "options": {"A": "Yes", "B": "No"},
                        "correct": "A",
                        "explanation": "Because.",
                    }
                ],
                "short": [
                    {"type": "short", "question": "Explain.", "answer": "Model."}
                ],
            }
        ],
        "sections": ["Section 2 - Fundamentals"],
    }


class CoreTests(unittest.TestCase):
    def test_natural_section_sorting(self) -> None:
        sections = ["Section 10 - Security", "Section 3 - Storage", "Section 2 - Fundamentals"]
        self.assertEqual(
            sorted(sections, key=section_sort_key),
            ["Section 2 - Fundamentals", "Section 3 - Storage", "Section 10 - Security"],
        )
        self.assertEqual(
            canonical_section_key("Section 10 - Security, Identity & Compliance"),
            canonical_section_key("Section 10 — Security Identity and Compliance"),
        )

    def test_validation_accepts_valid_data(self) -> None:
        data = valid_data()
        self.assertIs(validate_study_data(data), data)

    def test_validation_rejects_invalid_top_level_and_record(self) -> None:
        with self.assertRaisesRegex(StudyDataError, "top level"):
            validate_study_data([])
        malformed = valid_data()
        malformed["flashcards_qa"][0].pop("front")
        with self.assertRaisesRegex(StudyDataError, "front"):
            validate_study_data(malformed)

    def test_malformed_json_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.json"
            path.write_text('{"broken":', encoding="utf-8")
            with self.assertRaisesRegex(StudyDataError, "malformed JSON"):
                load_study_data(path)

    def test_missing_json_has_actionable_error(self) -> None:
        with self.assertRaisesRegex(StudyDataError, "src/build_data.py"):
            load_study_data(Path("/definitely/missing/study.json"))

    def test_data_file_version_changes_with_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.json"
            path.write_text(json.dumps(valid_data()), encoding="utf-8")
            first = data_file_version(path)
            os.utime(path, ns=(first + 10_000, first + 10_000))
            self.assertNotEqual(first, data_file_version(path))

    def test_stable_card_id_uses_deck_section_and_content(self) -> None:
        card = valid_data()["flashcards_qa"][0]
        first = stable_card_id("flashcards_qa", card)
        self.assertEqual(first, stable_card_id("flashcards_qa", dict(card)))
        self.assertNotEqual(first, stable_card_id("flashcards_terms", card))
        changed = dict(card, back="Different")
        self.assertNotEqual(first, stable_card_id("flashcards_qa", changed))

    def test_persistent_filtered_shuffle(self) -> None:
        state: dict = {}
        signature = make_filter_signature("deck", ["2"], [], "s3", "all")
        original = [f"id-{number}" for number in range(12)]
        shuffled = ensure_review_queue(
            state, signature, original, reshuffle=True, rng=random.Random(4)
        ).copy()
        self.assertNotEqual(original, shuffled)
        state["fc_index"] = 4
        after_next_rerun = ensure_review_queue(state, signature, original)
        self.assertEqual(shuffled, after_next_rerun)
        self.assertEqual(after_next_rerun[3], shuffled[3])

    def test_equal_count_filter_change_rebuilds_queue(self) -> None:
        state: dict = {}
        first_signature = make_filter_signature("deck", ["2"], [], "", "all")
        second_signature = make_filter_signature("deck", ["3"], [], "", "all")
        ensure_review_queue(
            state,
            first_signature,
            ["a", "b"],
            reshuffle=True,
            rng=random.Random(2),
        )
        rebuilt = ensure_review_queue(state, second_signature, ["c", "d"])
        self.assertEqual(rebuilt, ["c", "d"])
        self.assertEqual(state["fc_index"], 0)

    def test_search_includes_tags_sections_and_domain_names(self) -> None:
        card = {
            **valid_data()["flashcards_qa"][0],
            "_deck": "flashcards_qa",
            "_section_key": canonical_section_key("Section 2 - Fundamentals"),
            "_domains": (1,),
        }
        for query in ("section 2", "domain-1", "data ingestion", "fundamentals"):
            with self.subTest(query=query):
                self.assertEqual(
                    len(filter_cards([card], deck="flashcards_qa", query=query)), 1
                )
        self.assertEqual(
            filter_cards([card], deck="flashcards_qa", query="not present"), []
        )

    def test_html_like_content_is_escaped(self) -> None:
        text = "aws/<service> <script>alert('x')</script> & a > b"
        escaped = escape_markdown_text(text)
        self.assertNotIn("<script>", escaped)
        self.assertIn("&lt;service&gt;", escaped)
        self.assertIn("&amp;", escaped)
        self.assertIn("&gt;", escaped)

    def test_quiz_submit_locks_snapshot(self) -> None:
        state: dict = {}
        session = prepare_quiz_state(state, "quiz-a", 2)
        answers = {0: "A", 1: "B"}
        self.assertTrue(submit_quiz_state(session, answers))
        answers[0] = "B"
        self.assertEqual(session["answers"], {0: "A", 1: "B"})
        self.assertEqual(session["status"], "submitted")

    def test_quiz_submit_rejects_unanswered(self) -> None:
        session = prepare_quiz_state({}, "quiz-a", 2)
        self.assertFalse(submit_quiz_state(session, {0: "A"}))
        self.assertEqual(session["status"], "in_progress")

    def test_retake_clears_widget_keys(self) -> None:
        state = {
            "quiz_answer_0": "A",
            "short_response_0": "text",
            "other": "keep",
        }
        retake_quiz_state(state, "quiz-a", 1)
        self.assertNotIn("quiz_answer_0", state)
        self.assertNotIn("short_response_0", state)
        self.assertEqual(state["other"], "keep")

    def test_topic_switch_does_not_leak_answers(self) -> None:
        state: dict = {"quiz_answer_0": "A"}
        prepare_quiz_state(state, "quiz-a", 1)
        state["quiz_answer_0"] = "B"
        prepare_quiz_state(state, "quiz-b", 1)
        self.assertNotIn("quiz_answer_0", state)
        self.assertEqual(state["quiz_session"]["quiz_id"], "quiz-b")

    def test_mock_sample_uses_official_domain_weights(self) -> None:
        questions = [
            {"_id": f"{domain}-{index}", "domain": domain, "section": f"Section {domain}"}
            for domain in range(1, 5)
            for index in range(40)
        ]
        sample, weighted = weighted_mock_sample(questions, 50, random.Random(3))
        counts = {
            domain: sum(question["domain"] == domain for question in sample)
            for domain in range(1, 5)
        }
        self.assertTrue(weighted)
        self.assertEqual(counts, {1: 17, 2: 13, 3: 11, 4: 9})


if __name__ == "__main__":
    unittest.main()
