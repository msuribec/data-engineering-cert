"""Serialize and restore the safe, durable subset of Streamlit session state."""

from __future__ import annotations

import copy
from collections.abc import Mapping, MutableMapping
from typing import Any


SESSION_PAYLOAD_VERSION = 1

PERSISTED_KEYS = {
    "main_nav",
    "fc_deck",
    "fc_view",
    "fc_sections",
    "fc_domains",
    "fc_search",
    "fc_filter_signature",
    "fc_queue_ids",
    "fc_index",
    "fc_show_answer",
    "quiz_section",
    "quiz_topic",
    "quiz_session",
    "mock_session",
    "mistake_question_id",
    "mistake_answer",
    "mistake_result",
}

PERSISTED_PREFIXES = ("quiz_answer_", "short_response_", "mock_answer_")


def _integer_keys(value: Any) -> Any:
    """Restore integer-indexed answer mappings after a JSON round trip."""
    if not isinstance(value, dict):
        return value
    converted: dict[Any, Any] = {}
    for key, item in value.items():
        converted[int(key) if isinstance(key, str) and key.isdigit() else key] = item
    return converted


def capture_session_state(state: Mapping[str, Any]) -> dict[str, Any]:
    """Capture study workflow state without persisting source question content."""
    values: dict[str, Any] = {}
    for key, value in state.items():
        if key in PERSISTED_KEYS or key.startswith(PERSISTED_PREFIXES):
            values[key] = copy.deepcopy(value)

    mock = values.get("mock_session")
    if isinstance(mock, dict) and isinstance(mock.get("questions"), list):
        mock = copy.deepcopy(mock)
        mock["question_ids"] = [
            question.get("_id")
            for question in mock.pop("questions")
            if isinstance(question, dict) and question.get("_id")
        ]
        values["mock_session"] = mock

    return {"version": SESSION_PAYLOAD_VERSION, "state": values}


def restore_session_state(
    target: MutableMapping[str, Any],
    payload: Mapping[str, Any],
    question_lookup: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Restore a compatible snapshot, reconciling stable IDs with current data."""
    if payload.get("version") != SESSION_PAYLOAD_VERSION:
        return False
    values = payload.get("state")
    if not isinstance(values, dict):
        return False

    restored = copy.deepcopy(values)
    quiz = restored.get("quiz_session")
    if isinstance(quiz, dict):
        quiz["answers"] = _integer_keys(quiz.get("answers", {}))

    mock = restored.get("mock_session")
    if isinstance(mock, dict) and mock.get("status") != "setup":
        question_ids = mock.pop("question_ids", [])
        if not isinstance(question_ids, list) or any(
            question_id not in question_lookup for question_id in question_ids
        ):
            restored["mock_session"] = {"status": "setup"}
            for key in list(restored):
                if key.startswith("mock_answer_"):
                    del restored[key]
        else:
            mock["questions"] = [dict(question_lookup[question_id]) for question_id in question_ids]
            mock["answers"] = _integer_keys(mock.get("answers", {}))

    queue = restored.get("fc_queue_ids")
    if queue is not None and not isinstance(queue, list):
        restored.pop("fc_queue_ids", None)
        restored.pop("fc_filter_signature", None)
        restored["fc_index"] = 0

    for key, value in restored.items():
        target[key] = value
    return True


def clear_working_session_state(state: MutableMapping[str, Any]) -> None:
    """Clear workflow and widget state while preserving unrelated Streamlit state."""
    for key in list(state):
        if (
            key in PERSISTED_KEYS
            or key.startswith(PERSISTED_PREFIXES)
            or key.startswith("_session_")
        ):
            del state[key]
