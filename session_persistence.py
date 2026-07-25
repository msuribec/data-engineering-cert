"""Serialize and restore the safe, durable subset of Streamlit session state."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, MutableMapping
from typing import Any, Protocol

from study_core import compact_review_queue


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
    "fc_queue_order",
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
SAVED_DIGEST_KEY = "_session_saved_digest"


class ActiveSessionStore(Protocol):
    """Minimal persistence contract used by session checkpointing."""

    def save_active_session(
        self, payload: Mapping[str, Any], data_fingerprint: str
    ) -> None: ...


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
        if key not in PERSISTED_KEYS and not key.startswith(PERSISTED_PREFIXES):
            continue
        if key in {"fc_queue_ids", "fc_queue_order"}:
            continue
        if (
            key == "mock_session"
            and isinstance(value, dict)
            and isinstance(value.get("questions"), list)
        ):
            mock = {
                item_key: copy.deepcopy(item_value)
                for item_key, item_value in value.items()
                if item_key != "questions"
            }
            mock["question_ids"] = [
                question.get("_id")
                for question in value["questions"]
                if isinstance(question, dict) and question.get("_id")
            ]
            values[key] = mock
        else:
            values[key] = copy.deepcopy(value)

    queue = state.get("fc_queue_ids")
    if isinstance(queue, list) and all(isinstance(card_id, str) for card_id in queue):
        values["fc_queue_order"] = compact_review_queue(queue)
    elif isinstance(state.get("fc_queue_order"), Mapping):
        values["fc_queue_order"] = copy.deepcopy(state["fc_queue_order"])

    return {"version": SESSION_PAYLOAD_VERSION, "state": values}


def _canonical_json_value(value: Any) -> Any:
    """Normalize mapping keys so pre/post-JSON payloads hash identically."""
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    return value


def session_payload_digest(
    payload: Mapping[str, Any],
    data_fingerprint: str,
    session_scope: str = "",
) -> str:
    """Return a canonical digest for one data-version/session snapshot pair."""
    canonical = {
        "data_fingerprint": data_fingerprint,
        "session_scope": session_scope,
        "payload": _canonical_json_value(payload),
    }
    serialized = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def mark_session_payload_saved(
    state: MutableMapping[str, Any],
    payload: Mapping[str, Any],
    data_fingerprint: str,
    session_scope: str = "",
) -> None:
    """Seed the per-browser digest after restoring an existing checkpoint."""
    state[SAVED_DIGEST_KEY] = session_payload_digest(
        payload,
        data_fingerprint,
        session_scope,
    )


def persist_session_if_changed(
    store: ActiveSessionStore,
    state: MutableMapping[str, Any],
    data_fingerprint: str,
    session_scope: str = "",
) -> bool:
    """Persist only a changed durable snapshot; return whether a write occurred."""
    payload = capture_session_state(state)
    digest = session_payload_digest(payload, data_fingerprint, session_scope)
    if state.get(SAVED_DIGEST_KEY) == digest:
        return False
    store.save_active_session(payload, data_fingerprint)
    state[SAVED_DIGEST_KEY] = digest
    return True


def ensure_session_scope(
    state: MutableMapping[str, Any], session_scope: str
) -> bool:
    """Clear working state when the authenticated user or backend changes."""
    if state.get("_session_scope") == session_scope:
        return False
    clear_working_session_state(state)
    state["_session_scope"] = session_scope
    return True


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
        restored.pop("fc_queue_order", None)
        restored.pop("fc_filter_signature", None)
        restored["fc_index"] = 0
    compact_queue = restored.get("fc_queue_order")
    if compact_queue is not None and not isinstance(compact_queue, dict):
        restored.pop("fc_queue_order", None)
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
