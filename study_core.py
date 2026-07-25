"""Pure data, identity, filtering, queue, and quiz-state helpers for the study app."""

from __future__ import annotations

import hashlib
import html
import json
import random
import re
import unicodedata
from collections.abc import Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any


DECK_LABELS = {
    "flashcards_qa": "Q/A",
    "flashcards_services": "AWS services",
    "flashcards_terms": "Key terms",
}

DOMAIN_NAMES = {
    1: "Data Ingestion & Transformation",
    2: "Data Store Management",
    3: "Data Operations & Support",
    4: "Data Security & Governance",
}

DOMAIN_WEIGHTS = {1: 0.34, 2: 0.26, 3: 0.22, 4: 0.18}


class StudyDataError(Exception):
    """An actionable problem reading or validating the generated study data."""


def normalize_search(value: str) -> str:
    """Normalize Unicode and whitespace for case-insensitive matching."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def escape_markdown_text(value: str) -> str:
    """Make dynamic study text literal in a native Markdown element."""
    escaped_markdown = re.sub(r"([\\`*_{}\[\]()#+.!|\-])", r"\\\1", value)
    return html.escape(escaped_markdown, quote=False)


def canonical_section_key(label: str) -> str:
    """Return a punctuation-insensitive key for equivalent section labels."""
    normalized = normalize_search(label).replace("&", " and ")
    number = re.search(r"\bsection\s+(\d+)\b", normalized)
    words = re.sub(r"[^a-z0-9]+", " ", normalized)
    words = re.sub(r"^\s*section\s+\d+\s*", "", words).strip()
    return f"{int(number.group(1)) if number else 10_000}:{words}"


def section_sort_key(label: str) -> tuple[int, str]:
    """Sort course sections naturally by section number."""
    number = re.search(r"\bsection\s+(\d+)\b", label, re.IGNORECASE)
    return (int(number.group(1)) if number else 10_000, normalize_search(label))


def extract_domains(tags: str, explicit: Any = None) -> tuple[int, ...]:
    """Extract real DEA-C01 domain numbers from generated metadata."""
    domains: set[int] = set()
    if isinstance(explicit, int) and explicit in DOMAIN_NAMES:
        domains.add(explicit)
    elif isinstance(explicit, list):
        domains.update(value for value in explicit if isinstance(value, int) and value in DOMAIN_NAMES)
    match = re.search(r"\bDomain-([1-4](?:/[1-4])*)\b", tags)
    if match:
        domains.update(int(value) for value in match.group(1).split("/"))
    return tuple(sorted(domains))


def stable_card_id(deck: str, card: Mapping[str, Any]) -> str:
    """Create a stable content-derived card identifier."""
    identity = {
        "deck": deck,
        "section": canonical_section_key(str(card.get("section", ""))),
        "front": unicodedata.normalize("NFKC", str(card.get("front", ""))),
        "back": unicodedata.normalize("NFKC", str(card.get("back", ""))),
    }
    payload = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_quiz_id(quiz: Mapping[str, Any]) -> str:
    """Create a stable quiz identifier from its section and topic."""
    payload = f"{canonical_section_key(str(quiz.get('section', '')))}\0{quiz.get('topic', '')}"
    return hashlib.sha256(unicodedata.normalize("NFKC", payload).encode("utf-8")).hexdigest()


def stable_question_id(quiz: Mapping[str, Any], question: Mapping[str, Any]) -> str:
    """Create a stable multiple-choice question identifier."""
    options = question.get("options", {})
    payload = {
        "quiz_id": stable_quiz_id(quiz),
        "question": unicodedata.normalize("NFKC", str(question.get("question", ""))),
        "options": sorted((str(key), str(value)) for key, value in options.items()),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def stable_short_answer_id(quiz: Mapping[str, Any], question: Mapping[str, Any]) -> str:
    """Create a stable short-answer question identifier."""
    payload = f"{stable_quiz_id(quiz)}\0{question.get('question', '')}"
    return hashlib.sha256(unicodedata.normalize("NFKC", payload).encode("utf-8")).hexdigest()


def validate_study_data(data: Any) -> dict[str, Any]:
    """Validate the generated JSON without silently dropping malformed records."""
    if not isinstance(data, dict):
        raise StudyDataError("The data file must contain a JSON object at the top level.")

    required = (
        "flashcards_qa",
        "flashcards_services",
        "flashcards_terms",
        "quizzes",
        "sections",
    )
    errors: list[str] = []
    for key in required:
        if key not in data:
            errors.append(f"missing collection '{key}'")
        elif not isinstance(data[key], list):
            errors.append(f"'{key}' must be a list")
    if errors:
        raise StudyDataError("Invalid study data: " + "; ".join(errors))

    for deck in required[:3]:
        for index, card in enumerate(data[deck]):
            path = f"{deck}[{index}]"
            if not isinstance(card, dict):
                errors.append(f"{path} must be an object")
                continue
            for field in ("front", "back", "tags", "section"):
                if not isinstance(card.get(field), str):
                    errors.append(f"{path}.{field} must be text")

    for index, section in enumerate(data["sections"]):
        if not isinstance(section, str) or not section.strip():
            errors.append(f"sections[{index}] must be non-empty text")

    for quiz_index, quiz in enumerate(data["quizzes"]):
        path = f"quizzes[{quiz_index}]"
        if not isinstance(quiz, dict):
            errors.append(f"{path} must be an object")
            continue
        for field in ("section", "topic", "title"):
            if not isinstance(quiz.get(field), str) or not quiz[field].strip():
                errors.append(f"{path}.{field} must be non-empty text")
        for collection in ("mc", "short"):
            if not isinstance(quiz.get(collection), list):
                errors.append(f"{path}.{collection} must be a list")
        if "domain" in quiz and quiz["domain"] not in DOMAIN_NAMES:
            errors.append(f"{path}.domain must be an integer from 1 to 4")
        if not isinstance(quiz.get("mc"), list) or not isinstance(quiz.get("short"), list):
            continue
        for question_index, question in enumerate(quiz["mc"]):
            qpath = f"{path}.mc[{question_index}]"
            if not isinstance(question, dict):
                errors.append(f"{qpath} must be an object")
                continue
            for field in ("question", "correct", "explanation"):
                if not isinstance(question.get(field), str):
                    errors.append(f"{qpath}.{field} must be text")
            options = question.get("options")
            if not isinstance(options, dict) or len(options) < 2:
                errors.append(f"{qpath}.options must contain at least two choices")
            elif any(not isinstance(k, str) or not isinstance(v, str) for k, v in options.items()):
                errors.append(f"{qpath}.options keys and values must be text")
            elif question.get("correct") not in options:
                errors.append(f"{qpath}.correct must identify an available option")
        for question_index, question in enumerate(quiz["short"]):
            qpath = f"{path}.short[{question_index}]"
            if not isinstance(question, dict):
                errors.append(f"{qpath} must be an object")
                continue
            for field in ("question", "answer"):
                if not isinstance(question.get(field), str):
                    errors.append(f"{qpath}.{field} must be text")

    if errors:
        preview = "; ".join(errors[:8])
        remainder = f"; plus {len(errors) - 8} more issue(s)" if len(errors) > 8 else ""
        raise StudyDataError(f"Invalid study data: {preview}{remainder}. Rebuild it with src/build_data.py.")
    return data


def load_study_data(path: Path) -> dict[str, Any]:
    """Read and validate study JSON with concise, actionable failures."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise StudyDataError(f"Study data was not found at {path}. Run: python src/build_data.py") from exc
    except PermissionError as exc:
        raise StudyDataError(f"Study data at {path} is not readable. Check its file permissions.") from exc
    except OSError as exc:
        raise StudyDataError(f"Study data could not be read: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StudyDataError(
            f"Study data contains malformed JSON near line {exc.lineno}. Run: python src/build_data.py"
        ) from exc
    return validate_study_data(parsed)


def data_file_version(path: Path) -> int:
    """Return the mtime input used to invalidate Streamlit's data cache."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError as exc:
        raise StudyDataError(f"Study data was not found at {path}. Run: python src/build_data.py") from exc
    except PermissionError as exc:
        raise StudyDataError(f"Study data at {path} is not readable. Check its file permissions.") from exc
    except OSError as exc:
        raise StudyDataError(f"Study data could not be inspected: {exc}") from exc


def enrich_cards(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return UI card copies with stable IDs and parsed metadata."""
    cards: list[dict[str, Any]] = []
    for deck, label in DECK_LABELS.items():
        for source in data[deck]:
            card = dict(source)
            card["_deck"] = deck
            card["_deck_label"] = label
            card["_id"] = stable_card_id(deck, source)
            card["_section_key"] = canonical_section_key(source["section"])
            card["_domains"] = extract_domains(source.get("tags", ""), source.get("domains"))
            cards.append(card)
    return cards


def filter_cards(
    cards: Sequence[Mapping[str, Any]],
    *,
    deck: str,
    section_keys: Sequence[str] = (),
    domains: Sequence[int] = (),
    query: str = "",
) -> list[dict[str, Any]]:
    """Filter copied card records across all supported searchable metadata."""
    section_filter = set(section_keys)
    domain_filter = set(domains)
    needle = normalize_search(query)
    results: list[dict[str, Any]] = []
    for source in cards:
        if source["_deck"] != deck:
            continue
        if section_filter and source["_section_key"] not in section_filter:
            continue
        if domain_filter and not domain_filter.intersection(source["_domains"]):
            continue
        if needle:
            domain_text = " ".join(
                f"Domain {number} {DOMAIN_NAMES[number]}" for number in source["_domains"]
            )
            haystack = normalize_search(
                " ".join(
                    str(source.get(field, ""))
                    for field in ("front", "back", "section", "tags", "service", "domain")
                )
                + " "
                + domain_text
            )
            if needle not in haystack:
                continue
        results.append(dict(source))
    return results


def make_filter_signature(
    deck: str,
    section_keys: Sequence[str],
    domains: Sequence[int],
    query: str,
    view: str,
) -> str:
    """Create a signature that changes even when two filters return equal counts."""
    payload = (
        deck,
        tuple(sorted(section_keys)),
        tuple(sorted(domains)),
        normalize_search(query),
        view,
    )
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


def ensure_review_queue(
    state: MutableMapping[str, Any],
    signature: str,
    card_ids: Sequence[str],
    *,
    reshuffle: bool = False,
    rng: random.Random | None = None,
) -> list[str]:
    """Maintain a stable queue until filters change or reshuffle is explicit."""
    available = list(card_ids)
    current = state.get("fc_queue_ids", [])
    queue_is_stale = (
        state.get("fc_filter_signature") != signature
        or not isinstance(current, list)
        or len(current) != len(available)
        or set(current) != set(available)
    )
    if queue_is_stale:
        current = available.copy()
        state["fc_index"] = 0
        state["fc_show_answer"] = False
    if reshuffle:
        current = available.copy()
        (rng or random.SystemRandom()).shuffle(current)
        state["fc_index"] = 0
        state["fc_show_answer"] = False
    state["fc_filter_signature"] = signature
    state["fc_queue_ids"] = current
    if current:
        state["fc_index"] = min(int(state.get("fc_index", 0)), len(current) - 1)
    else:
        state["fc_index"] = 0
    return current


QUIZ_WIDGET_PREFIXES = ("quiz_answer_", "short_response_", "short_reveal_")


def clear_quiz_widget_state(state: MutableMapping[str, Any]) -> None:
    """Remove quiz widget keys so topics and retakes cannot leak answers."""
    for key in list(state):
        if key.startswith(QUIZ_WIDGET_PREFIXES):
            del state[key]


def prepare_quiz_state(
    state: MutableMapping[str, Any], quiz_id: str, question_count: int
) -> dict[str, Any]:
    """Return a clean in-progress state, resetting it when the topic changes."""
    session = state.get("quiz_session")
    if not isinstance(session, dict) or session.get("quiz_id") != quiz_id:
        clear_quiz_widget_state(state)
        session = {
            "quiz_id": quiz_id,
            "status": "in_progress",
            "answers": {},
            "question_count": question_count,
            "review_missed": False,
            "revealed_short": [],
        }
        state["quiz_session"] = session
    return session


def submit_quiz_state(session: MutableMapping[str, Any], answers: Mapping[int, str]) -> bool:
    """Freeze a complete answer snapshot; return False when answers are missing."""
    count = int(session.get("question_count", 0))
    if count and any(not answers.get(index) for index in range(count)):
        return False
    session["answers"] = dict(answers)
    session["status"] = "submitted"
    return True


def retake_quiz_state(
    state: MutableMapping[str, Any], quiz_id: str, question_count: int
) -> dict[str, Any]:
    """Clear all relevant widgets and create a fresh quiz attempt."""
    clear_quiz_widget_state(state)
    session = {
        "quiz_id": quiz_id,
        "status": "in_progress",
        "answers": {},
        "question_count": question_count,
        "review_missed": False,
        "revealed_short": [],
    }
    state["quiz_session"] = session
    return session


def weighted_mock_sample(
    questions: Sequence[Mapping[str, Any]],
    count: int,
    rng: random.Random,
) -> tuple[list[dict[str, Any]], bool]:
    """Sample using official domain weights, or section balance if metadata is incomplete."""
    if count <= 0:
        return [], True
    available = list(questions)
    has_domains = all(question.get("domain") in DOMAIN_NAMES for question in available)
    selected: list[Mapping[str, Any]] = []
    if has_domains:
        quotas = {domain: int(count * weight) for domain, weight in DOMAIN_WEIGHTS.items()}
        assigned = sum(quotas.values())
        remainder_order = sorted(
            DOMAIN_WEIGHTS,
            key=lambda domain: (count * DOMAIN_WEIGHTS[domain] - quotas[domain]),
            reverse=True,
        )
        for domain in remainder_order[: count - assigned]:
            quotas[domain] += 1
        leftovers: list[Mapping[str, Any]] = []
        for domain in DOMAIN_NAMES:
            pool = [question for question in available if question.get("domain") == domain]
            rng.shuffle(pool)
            selected.extend(pool[: quotas[domain]])
            leftovers.extend(pool[quotas[domain] :])
        if len(selected) < count:
            rng.shuffle(leftovers)
            selected.extend(leftovers[: count - len(selected)])
    else:
        pools: dict[str, list[Mapping[str, Any]]] = {}
        for question in available:
            pools.setdefault(str(question.get("section", "")), []).append(question)
        for pool in pools.values():
            rng.shuffle(pool)
        while len(selected) < count and any(pools.values()):
            for section in sorted(pools, key=section_sort_key):
                if pools[section] and len(selected) < count:
                    selected.append(pools[section].pop())
    rng.shuffle(selected)
    return [dict(question) for question in selected[:count]], has_domains
