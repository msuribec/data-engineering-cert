#!/usr/bin/env python3
"""AWS DEA-C01 study application with local or deployed multi-user persistence.

Setup:
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

Launch:
    streamlit run app.py

Rebuild generated data:
    python src/build_data.py

The app reads data/study_data.json. Locally it uses a SQLite file under
.study_progress/. When DATABASE_URL and Streamlit OIDC secrets are configured,
it uses user-scoped Postgres persistence suitable for Streamlit Community Cloud.
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import streamlit as st

from progress_store import ProgressBackend, ProgressStore, from_iso, utc_now
from session_persistence import (
    clear_working_session_state,
    ensure_session_scope,
    mark_session_payload_saved,
    persist_session_if_changed,
    restore_session_state,
)
from study_core import (
    DECK_LABELS,
    DOMAIN_NAMES,
    StudyDataError,
    canonical_section_key,
    data_file_version,
    enrich_cards,
    ensure_review_queue,
    escape_markdown_text,
    filter_cards,
    load_study_data_snapshot,
    make_filter_signature,
    prepare_quiz_state,
    retake_quiz_state,
    section_sort_key,
    stable_question_id,
    stable_quiz_id,
    stable_short_answer_id,
    submit_quiz_state,
    weighted_mock_sample,
)


ROOT = Path(__file__).resolve().parent
DATA_PATH = Path(os.environ.get("DEA_STUDY_DATA_PATH", ROOT / "data" / "study_data.json"))
PROGRESS_PATH = Path(
    os.environ.get("DEA_STUDY_PROGRESS_PATH", ROOT / ".study_progress" / "progress.db")
)
NAV_ITEMS = ("Dashboard", "Flashcards", "Quizzes", "Mock Exam", "Review")


st.set_page_config(
    page_title="AWS DEA-C01 Study",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    :root { --study-navy: #232F3E; --study-orange: #FF9900; }
    .block-container { max-width: 1120px; padding-top: 1.5rem; padding-bottom: 3rem; }
    h1, h2, h3 { color: var(--study-navy); letter-spacing: -0.015em; }
    [data-testid="stMetric"] {
        border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
        border-radius: 0.75rem;
        padding: 0.75rem 1rem;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: color-mix(in srgb, currentColor 20%, transparent);
    }
    .st-key-flashcard_front {
        border-left: 0.3rem solid var(--study-navy);
    }
    .st-key-flashcard_answer {
        border-left: 0.3rem solid var(--study-orange);
    }
    @media (max-width: 640px) {
        .block-container { padding-left: 1rem; padding-right: 1rem; }
        h1 { font-size: 1.8rem; }
    }
    @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@dataclass(frozen=True)
class RuntimeData:
    """Immutable-by-convention data prepared once per generated-file version."""

    data: dict[str, Any]
    cards: list[dict[str, Any]]
    quizzes: list[dict[str, Any]]
    questions: list[dict[str, Any]]
    question_lookup: dict[str, dict[str, Any]]
    fingerprint: str


@st.cache_resource(show_spinner=False)
def load_data(path_text: str, modified_ns: int) -> RuntimeData:
    """Load, validate, enrich, index, and hash one generated-data version."""
    del modified_ns
    path = Path(path_text)
    data, fingerprint = load_study_data_snapshot(path)
    cards = enrich_cards(data)
    quizzes = decorate_quizzes(data)
    questions = all_questions(quizzes)
    return RuntimeData(
        data=data,
        cards=cards,
        quizzes=quizzes,
        questions=questions,
        question_lookup={question["_id"]: question for question in questions},
        fingerprint=fingerprint,
    )


@st.cache_resource(show_spinner=False)
def open_progress_backend(
    path_text: str, database_url: str | None
) -> ProgressBackend:
    """Open and initialize one process-wide storage backend."""
    return ProgressBackend(
        Path(path_text),
        database_url=database_url,
    )


def open_progress_store(
    path_text: str, database_url: str | None, user_id: str
) -> ProgressStore:
    """Create a lightweight learner facade over the shared backend."""
    backend = open_progress_backend(path_text, database_url)
    return ProgressStore(user_id=user_id, backend=backend)


@dataclass(frozen=True)
class UserContext:
    """Authenticated identity used to scope every persistent query."""

    user_id: str
    display_name: str
    email: str
    authentication_enabled: bool


def configuration() -> dict[str, Any]:
    """Read deployment configuration without requiring a local secrets file."""
    try:
        secrets = st.secrets.to_dict()
    except (FileNotFoundError, OSError):
        secrets = {}
    database = secrets.get("database", {})
    app_config = secrets.get("app", {})
    return {
        "database_url": (
            os.environ.get("DEA_STUDY_DATABASE_URL")
            or os.environ.get("DATABASE_URL")
            or (database.get("url") if isinstance(database, dict) else None)
        ),
        "auth_enabled": isinstance(secrets.get("auth"), dict),
        "allowed_emails": {
            str(email).strip().casefold()
            for email in (
                app_config.get("allowed_emails", [])
                if isinstance(app_config, dict)
                else []
            )
            if str(email).strip()
        },
    }


def require_user(config: Mapping[str, Any]) -> UserContext:
    """Require Google OIDC in cloud mode and return a stable, opaque user ID."""
    if not config["auth_enabled"]:
        if config["database_url"]:
            st.error(
                "A cloud database is configured without OIDC authentication. "
                "Add the [auth] settings described in deploy_steps.md."
            )
            st.stop()
        return UserContext("local", "Local learner", "", False)

    user = st.user.to_dict()
    if not user.get("is_logged_in"):
        st.subheader("Sign in to continue")
        st.caption("Your progress and active study session are private to your account.")
        st.button("Sign in with Google", type="primary", on_click=st.login)
        st.stop()

    subject = str(user.get("sub", "")).strip()
    issuer = str(user.get("iss", "")).strip()
    email = str(user.get("email", "")).strip()
    if not subject or not issuer:
        st.error("The identity provider did not return the required issuer and subject claims.")
        st.button("Sign out", on_click=st.logout)
        st.stop()
    allowed = config["allowed_emails"]
    if allowed and email.casefold() not in allowed:
        st.error("This Google account is not authorized to use the study app.")
        st.button("Sign out", on_click=st.logout)
        st.stop()
    opaque_id = hashlib.sha256(f"{issuer}\0{subject}".encode("utf-8")).hexdigest()
    name = str(user.get("name") or email or "Learner")
    return UserContext(opaque_id, name, email, True)


def make_session_scope(user: UserContext, store: ProgressStore) -> str:
    """Hash identity and backend location without retaining database credentials."""
    location = store.database_url or str(store.path.resolve())
    material = f"{user.user_id}\0{store.backend}\0{location}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def start_fresh_session(store: ProgressStore, session_scope: str) -> None:
    """Discard working state without deleting long-term study progress."""
    store.clear_active_session()
    clear_working_session_state(st.session_state)
    st.session_state["_session_scope"] = session_scope
    st.session_state["_session_initialized"] = True
    st.session_state["_session_started_fresh"] = True


def literal(value: Any) -> None:
    """Render dynamic content as inert, literal Markdown text."""
    st.markdown(escape_markdown_text(str(value)))


def format_when(value: str | None) -> str:
    """Format a stored UTC timestamp for a compact reader-facing label."""
    parsed = from_iso(value)
    if parsed is None:
        return "Not scheduled"
    local = parsed.astimezone()
    now = utc_now()
    seconds = (parsed - now).total_seconds()
    if seconds <= 0:
        return "Due now"
    if seconds < 3600:
        return f"Due in {max(1, round(seconds / 60))} min"
    if seconds < 86_400:
        return f"Due in {round(seconds / 3600)} hr"
    return f"Due {local.strftime('%b %-d')}"


def section_display_map(data: Mapping[str, Any], cards: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Prefer the generated quiz label while merging punctuation-only variants."""
    labels: dict[str, str] = {}
    for label in data["sections"]:
        labels[canonical_section_key(label)] = label
    for card in cards:
        labels.setdefault(card["_section_key"], card["section"])
    return labels


def domain_label(domain: int) -> str:
    return f"Domain {domain} — {DOMAIN_NAMES[domain]}"


def go_to(page: str) -> None:
    """Navigate using the single top-level navigation state."""
    st.session_state["main_nav"] = page


def continue_due_cards() -> None:
    """Prepare the Due view and navigate before widgets instantiate."""
    st.session_state["fc_view"] = "Due today"
    go_to("Flashcards")


def clear_flashcard_filters() -> None:
    """Reset filter widgets without touching learner progress."""
    for key in ("fc_sections", "fc_domains"):
        st.session_state[key] = []
    st.session_state["fc_search"] = ""
    st.session_state["fc_view"] = "All matching cards"


def render_service_answer(back: str) -> None:
    """Preserve the service-card Purpose/When-to-use structure safely."""
    parts = [part.strip() for part in back.split(" | ") if part.strip()]
    for part in parts:
        heading, separator, content = part.partition(":")
        if separator and heading.strip().casefold() in {"purpose", "when to use / notes"}:
            st.markdown(f"**{heading.strip()}**")
            literal(content.strip())
        else:
            literal(part)


def render_card_text(card: Mapping[str, Any], answer: bool) -> None:
    """Render a card without injecting source content into raw HTML."""
    if answer and card["_deck"] == "flashcards_services":
        render_service_answer(card["back"])
        return
    text = card["back"] if answer else card["front"]
    parts = [part.strip() for part in text.split(" | ") if part.strip()]
    if len(parts) <= 1:
        literal(text)
    else:
        for part in parts:
            st.markdown("- " + escape_markdown_text(part))


def decorate_quizzes(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Copy quizzes and attach stable runtime IDs."""
    quizzes: list[dict[str, Any]] = []
    for source in data["quizzes"]:
        quiz = dict(source)
        quiz["_id"] = stable_quiz_id(source)
        quiz["mc"] = [dict(question) for question in source["mc"]]
        for question in quiz["mc"]:
            question["_id"] = stable_question_id(source, question)
        quiz["short"] = [dict(question) for question in source["short"]]
        for question in quiz["short"]:
            question["_id"] = stable_short_answer_id(source, question)
        quizzes.append(quiz)
    return quizzes


def all_questions(quizzes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten multiple-choice questions with their trustworthy source metadata."""
    questions: list[dict[str, Any]] = []
    for quiz in quizzes:
        for source in quiz["mc"]:
            question = dict(source)
            question.update(
                {
                    "section": quiz["section"],
                    "topic": quiz["topic"],
                    "domain": quiz.get("domain"),
                    "_quiz_id": quiz["_id"],
                }
            )
            questions.append(question)
    return questions


def render_dashboard(
    store: ProgressStore,
    cards: Sequence[Mapping[str, Any]],
) -> None:
    st.header("Dashboard")
    stats = store.dashboard_stats([card["_id"] for card in cards])
    if not stats["has_activity"]:
        st.info(
            "Your study history is empty. Review a flashcard or complete a quiz to start "
            "building progress insights."
        )

    columns = st.columns(4)
    columns[0].metric("Cards reviewed", stats["total_reviewed"])
    columns[1].metric("Cards due now", stats["due"])
    columns[2].metric(
        "Quiz average",
        "—" if stats["quiz_average"] is None else f"{stats['quiz_average']:.0f}%",
    )
    recent = stats["recent_quiz"]
    columns[3].metric(
        "Recent quiz",
        "—" if recent is None else f"{recent['score']}/{recent['total']}",
    )

    if stats["has_activity"]:
        insight_columns = st.columns(2)
        strongest = stats["strongest"]
        weakest = stats["weakest"]
        with insight_columns[0]:
            st.subheader("Strongest section")
            if strongest:
                literal(f"{strongest['section']} · {strongest['average']:.0f}%")
            else:
                st.caption("Complete a quiz to calculate this.")
        with insight_columns[1]:
            st.subheader("Weakest section")
            if weakest:
                literal(f"{weakest['section']} · {weakest['average']:.0f}%")
            else:
                st.caption("Complete a quiz to calculate this.")

    action_columns = st.columns(2)
    action_columns[0].button(
        "Continue with due cards",
        type="primary",
        width="stretch",
        on_click=continue_due_cards,
    )
    action_columns[1].button(
        "Review mistakes",
        width="stretch",
        on_click=go_to,
        args=("Review",),
    )

    st.subheader("Recent activity")
    if not stats["recent_activity"]:
        st.caption("No reviews or quiz attempts yet.")
    for event in stats["recent_activity"]:
        icon = "Flashcard" if event["kind"] == "card" else "Quiz"
        literal(f"{icon} · {event['section']} · {event['detail']}")

    if st.session_state.pop("_clear_reset_confirmation", False):
        st.session_state["reset_progress_confirmed"] = False
    with st.expander("Reset my progress"):
        st.warning(
            "This permanently removes ratings, schedules, bookmarks, quiz attempts, "
            "mistakes, saved sessions, and short-answer self-ratings for your account."
        )
        confirmed = st.checkbox(
            "I understand that all of my stored study progress will be deleted",
            key="reset_progress_confirmed",
        )
        if st.button("Reset all progress", disabled=not confirmed):
            store.reset_all_progress()
            st.session_state["_clear_reset_confirmation"] = True
            st.session_state["_session_reset_requested"] = True
            st.success("Your progress was reset.")
            st.rerun()


def render_flashcards(
    data: Mapping[str, Any],
    store: ProgressStore,
    cards: Sequence[Mapping[str, Any]],
) -> None:
    st.header("Flashcards")
    labels = section_display_map(data, cards)
    section_options = sorted(labels, key=lambda key: section_sort_key(labels[key]))

    st.session_state.setdefault("fc_view", "All matching cards")
    filter_columns = st.columns((1.1, 1.5, 1.3))
    with filter_columns[0]:
        deck = st.selectbox(
            "Deck",
            list(DECK_LABELS),
            format_func=lambda value: DECK_LABELS[value],
            key="fc_deck",
        )
        view = st.selectbox(
            "Review view",
            ("Due today", "New cards", "Bookmarked cards", "All matching cards"),
            key="fc_view",
        )
    with filter_columns[1]:
        selected_sections = st.multiselect(
            "Sections",
            section_options,
            format_func=lambda value: labels[value],
            key="fc_sections",
        )
        selected_domains = st.multiselect(
            "Exam domains",
            list(DOMAIN_NAMES),
            format_func=domain_label,
            key="fc_domains",
        )
    with filter_columns[2]:
        query = st.text_input(
            "Search cards",
            placeholder="Front, answer, section, tags, service, or domain",
            key="fc_search",
        )
        st.button(
            "Clear filters",
            width="stretch",
            on_click=clear_flashcard_filters,
        )

    matching = filter_cards(
        cards,
        deck=deck,
        section_keys=selected_sections,
        domains=selected_domains,
        query=query,
    )
    progress = store.get_card_progress([card["_id"] for card in matching])
    now = utc_now()
    due_count = sum(
        1
        for card in matching
        if progress.get(card["_id"], {}).get("review_count", 0) > 0
        and (from_iso(progress[card["_id"]].get("next_due")) or now) <= now
    )
    if view == "Due today":
        visible = [
            card
            for card in matching
            if progress.get(card["_id"], {}).get("review_count", 0) > 0
            and (from_iso(progress[card["_id"]].get("next_due")) or now) <= now
        ]
    elif view == "New cards":
        visible = [
            card for card in matching if progress.get(card["_id"], {}).get("review_count", 0) == 0
        ]
    elif view == "Bookmarked cards":
        visible = [
            card for card in matching if bool(progress.get(card["_id"], {}).get("bookmarked"))
        ]
    else:
        visible = matching

    active: list[str] = [DECK_LABELS[deck], view]
    active.extend(labels[key] for key in selected_sections)
    active.extend(f"Domain {number}" for number in selected_domains)
    if query.strip():
        active.append(f'Search: “{query.strip()}”')
    with st.container(horizontal=True, gap="small"):
        for item in active:
            st.badge(item, color="gray")

    next_dates = [
        from_iso(record.get("next_due"))
        for record in progress.values()
        if record.get("next_due") and (from_iso(record.get("next_due")) or now) > now
    ]
    next_due = min((date for date in next_dates if date), default=None)
    st.caption(
        f"{len(visible)} cards in this view · {due_count} due now"
        + (f" · Next scheduled {next_due.astimezone().strftime('%b %-d, %-I:%M %p')}" if next_due else "")
    )

    signature = make_filter_signature(
        deck, selected_sections, selected_domains, query, view
    )
    action_columns = st.columns((1, 1, 3))
    reshuffle = action_columns[0].button("Shuffle queue", width="stretch")
    if action_columns[1].button("Start from first", width="stretch"):
        st.session_state["fc_index"] = 0
        st.session_state["fc_show_answer"] = False
        st.rerun()
    queue = ensure_review_queue(
        st.session_state,
        signature,
        [card["_id"] for card in visible],
        reshuffle=reshuffle,
    )

    if not queue:
        st.info(
            "No cards match this view. Try All matching cards, clear a filter, or review "
            "new cards first."
        )
        return

    card_by_id = {card["_id"]: card for card in visible}
    index = int(st.session_state.get("fc_index", 0)) % len(queue)
    card = card_by_id[queue[index]]
    record = progress.get(card["_id"], {})
    show_answer = bool(st.session_state.get("fc_show_answer", False))

    st.progress((index + 1) / len(queue), text=f"Card {index + 1} of {len(queue)}")
    status = (
        "New"
        if not record.get("review_count")
        else f"{record['review_count']} reviews · {format_when(record.get('next_due'))}"
    )
    with st.container(horizontal=True, gap="small"):
        st.badge(card["_deck_label"], color="blue")
        st.badge(labels.get(card["_section_key"], card["section"]), color="gray")
        for number in card["_domains"]:
            st.badge(f"Domain {number}", color="orange")
        st.badge(status, color="green" if record.get("review_count") else "gray")

    with st.container(
        border=True,
        key="flashcard_answer" if show_answer else "flashcard_front",
    ):
        st.caption("ANSWER" if show_answer else "FRONT")
        render_card_text(card, show_answer)

    bookmark_label = "Remove bookmark" if record.get("bookmarked") else "Bookmark"
    nav_columns = st.columns((1, 1, 1, 1))
    if nav_columns[0].button("Previous", width="stretch"):
        st.session_state["fc_index"] = (index - 1) % len(queue)
        st.session_state["fc_show_answer"] = False
        st.rerun()
    if nav_columns[1].button(
        "Show front" if show_answer else "Reveal answer",
        type="primary",
        width="stretch",
    ):
        st.session_state["fc_show_answer"] = not show_answer
        st.rerun()
    if nav_columns[2].button("Next", width="stretch"):
        st.session_state["fc_index"] = (index + 1) % len(queue)
        st.session_state["fc_show_answer"] = False
        st.rerun()
    if nav_columns[3].button(bookmark_label, width="stretch"):
        store.set_bookmark(
            card["_id"], card["section"], card["_deck"], not bool(record.get("bookmarked"))
        )
        st.rerun()

    if show_answer:
        st.markdown("#### How well did you remember it?")
        rating_columns = st.columns(4)
        rating_help = {
            "Again": "10 min",
            "Hard": "Soon",
            "Good": "Normal",
            "Easy": "Later",
        }
        for column, rating in zip(rating_columns, rating_help, strict=True):
            if column.button(
                f"{rating}\n\n{rating_help[rating]}",
                key=f"rate_{rating.lower()}",
                width="stretch",
            ):
                store.rate_card(
                    card["_id"], card["section"], card["_deck"], rating
                )
                st.session_state["fc_index"] = (index + 1) % len(queue)
                st.session_state["fc_show_answer"] = False
                st.rerun()
    st.caption(
        "Keyboard shortcuts are not enabled because Streamlit has no reliable native, "
        "focus-safe key event API. The visible controls remain keyboard accessible."
    )


def record_quiz_result(
    store: ProgressStore,
    quiz: Mapping[str, Any],
    answers: Mapping[int, str],
) -> tuple[int, list[str]]:
    """Persist one immutable quiz attempt and its mistake identifiers."""
    correct_ids: list[str] = []
    incorrect_ids: list[str] = []
    for index, question in enumerate(quiz["mc"]):
        target = correct_ids if answers.get(index) == question["correct"] else incorrect_ids
        target.append(question["_id"])
    store.record_quiz_attempt(
        quiz_id=quiz["_id"],
        section=quiz["section"],
        topic=quiz["topic"],
        score=len(correct_ids),
        total=len(quiz["mc"]),
        incorrect_ids=incorrect_ids,
        correct_ids=correct_ids,
    )
    return len(correct_ids), incorrect_ids


def render_short_answers(
    store: ProgressStore,
    quiz: Mapping[str, Any],
    session: dict[str, Any],
) -> None:
    if not quiz["short"]:
        return
    st.subheader("Short-answer practice")
    st.caption("Write your own response, reveal the model answer, then self-rate it.")
    revealed = set(session.get("revealed_short", []))
    for index, question in enumerate(quiz["short"]):
        with st.container(border=True):
            st.markdown(f"**Short answer {index + 1}**")
            literal(question["question"])
            response = st.text_area(
                f"Your response to short-answer question {index + 1}",
                key=f"short_response_{index}",
            )
            if st.button(
                "Reveal model answer",
                key=f"short_reveal_{index}",
                disabled=index in revealed,
            ):
                revealed.add(index)
                session["revealed_short"] = sorted(revealed)
            if index in revealed:
                st.markdown("**Model answer**")
                literal(question["answer"] or "No model answer was captured.")
                columns = st.columns(2)
                for column, rating in zip(columns, ("Needs review", "Understood"), strict=True):
                    if column.button(
                        rating,
                        key=f"short_rate_{index}_{rating}",
                        width="stretch",
                    ):
                        store.record_short_answer(
                            question_id=question["_id"],
                            quiz_id=quiz["_id"],
                            section=quiz["section"],
                            rating=rating,
                            response=response,
                        )
                        st.success(f"Saved self-rating: {rating}")


def render_quizzes(
    data: Mapping[str, Any],
    store: ProgressStore,
    quizzes: Sequence[Mapping[str, Any]],
) -> None:
    st.header("Quizzes")
    sections = sorted(data["sections"], key=section_sort_key)
    selection_columns = st.columns(2)
    section = selection_columns[0].selectbox("Quiz section", sections, key="quiz_section")
    topics = [quiz for quiz in quizzes if canonical_section_key(quiz["section"]) == canonical_section_key(section)]
    topic_names = [quiz["topic"] for quiz in topics]
    topic = selection_columns[1].selectbox("Quiz topic", topic_names, key="quiz_topic")
    quiz = next(quiz for quiz in topics if quiz["topic"] == topic)
    session = prepare_quiz_state(st.session_state, quiz["_id"], len(quiz["mc"]))
    submitted = session["status"] == "submitted"

    literal(quiz["title"])
    st.caption(
        f"{quiz['section']} · Domain {quiz.get('domain', '—')} · "
        f"{len(quiz['mc'])} multiple choice · {len(quiz['short'])} short answer"
    )

    if submitted:
        answers = session["answers"]
        score = sum(
            answers.get(index) == question["correct"]
            for index, question in enumerate(quiz["mc"])
        )
        total = len(quiz["mc"])
        percentage = round(100 * score / total) if total else 0
        st.metric("Quiz score", f"{score} / {total}", f"{percentage}%")
        missed_indices = [
            index
            for index, question in enumerate(quiz["mc"])
            if answers.get(index) != question["correct"]
        ]
        action_columns = st.columns(2)
        if action_columns[0].button("Retake quiz", type="primary", width="stretch"):
            retake_quiz_state(st.session_state, quiz["_id"], len(quiz["mc"]))
            st.rerun()
        review_label = (
            "Show all questions" if session.get("review_missed") else "Review missed questions"
        )
        if action_columns[1].button(
            review_label,
            disabled=not missed_indices,
            width="stretch",
        ):
            session["review_missed"] = not session.get("review_missed", False)
            st.rerun()
        shown_indices = missed_indices if session.get("review_missed") else range(len(quiz["mc"]))
    else:
        answers = {
            index: st.session_state.get(f"quiz_answer_{index}")
            for index in range(len(quiz["mc"]))
        }
        answered = sum(bool(answer) for answer in answers.values())
        st.progress(
            answered / len(quiz["mc"]) if quiz["mc"] else 0,
            text=f"Answered {answered} of {len(quiz['mc'])}",
        )
        if not quiz["mc"]:
            st.info("This topic has no multiple-choice questions; use the short-answer practice below.")
        shown_indices = range(len(quiz["mc"]))

    for index in shown_indices:
        question = quiz["mc"][index]
        with st.container(border=True):
            st.markdown(f"**Question {index + 1} of {len(quiz['mc'])}**")
            literal(question["question"])
            letters = list(question["options"])
            choice = st.radio(
                f"Answer for question {index + 1}",
                letters,
                format_func=lambda letter, options=question["options"]: (
                    f"{letter}) {options[letter]}"
                ),
                index=None,
                key=f"quiz_answer_{index}",
                disabled=submitted,
            )
            if not submitted:
                answers[index] = choice
            else:
                selected = session["answers"].get(index)
                correct = question["correct"]
                status_text = "Correct" if selected == correct else "Incorrect"
                st.markdown(f"**{status_text}**")
                st.markdown("**Your selected answer**")
                literal(
                    "Not answered"
                    if selected is None
                    else f"{selected}) {question['options'][selected]}"
                )
                st.markdown("**Correct answer**")
                literal(f"{correct}) {question['options'][correct]}")
                st.markdown("**Explanation**")
                literal(question["explanation"] or "No explanation was captured.")

    if not submitted and quiz["mc"]:
        answers = {
            index: st.session_state.get(f"quiz_answer_{index}")
            for index in range(len(quiz["mc"]))
        }
        answered = sum(bool(answer) for answer in answers.values())
        if st.button(
            "Submit quiz",
            type="primary",
            disabled=answered != len(quiz["mc"]),
            width="stretch",
        ):
            if submit_quiz_state(session, answers):
                record_quiz_result(store, quiz, session["answers"])
                st.rerun()
        if answered != len(quiz["mc"]):
            st.caption("Answer every multiple-choice question to enable submission.")

    render_short_answers(store, quiz, session)


def clear_mock_widgets() -> None:
    for key in list(st.session_state):
        if key.startswith("mock_answer_"):
            del st.session_state[key]


def render_mock_exam(
    store: ProgressStore,
    questions: Sequence[Mapping[str, Any]],
) -> None:
    st.header("Mock Exam")
    session = st.session_state.get("mock_session")
    if not isinstance(session, dict):
        session = {"status": "setup"}
        st.session_state["mock_session"] = session

    if session["status"] == "setup":
        max_questions = min(100, len(questions))
        count = st.slider(
            "Number of questions",
            min_value=5,
            max_value=max_questions,
            value=min(25, max_questions),
            step=5,
        )
        randomize_choices = st.checkbox("Randomize answer-choice order", value=True)
        timer_minutes = st.selectbox(
            "Optional timer",
            (0, 30, 60, 90),
            format_func=lambda value: "No timer" if value == 0 else f"{value} minutes",
        )
        st.caption(
            "Questions use the official 34% / 26% / 22% / 18% domain weighting because "
            "each source study sheet contains an explicit primary-domain mapping."
        )
        if st.button("Start mock exam", type="primary"):
            seed = time.time_ns()
            selected, weighted = weighted_mock_sample(
                questions, count, random.Random(seed)
            )
            choice_orders: dict[str, list[str]] = {}
            for question in selected:
                letters = list(question["options"])
                if randomize_choices:
                    random.Random(f"{seed}:{question['_id']}").shuffle(letters)
                choice_orders[question["_id"]] = letters
            clear_mock_widgets()
            st.session_state["mock_session"] = {
                "status": "in_progress",
                "questions": selected,
                "answers": {},
                "choice_orders": choice_orders,
                "started_at": time.time(),
                "timer_minutes": timer_minutes,
                "weighted": weighted,
                "review_missed": False,
            }
            st.rerun()
        return

    selected = session["questions"]
    submitted = session["status"] == "submitted"
    elapsed = time.time() - session["started_at"]
    timer_seconds = session["timer_minutes"] * 60
    expired = bool(timer_seconds and elapsed >= timer_seconds)
    if timer_seconds:
        remaining = max(0, timer_seconds - int(elapsed))
        st.caption(f"Timer · {remaining // 60:02d}:{remaining % 60:02d} remaining")
    else:
        st.caption("Untimed exam")

    if submitted:
        answers = session["answers"]
        correct = [
            question for index, question in enumerate(selected)
            if answers.get(index) == question["correct"]
        ]
        incorrect = [
            question for index, question in enumerate(selected)
            if answers.get(index) != question["correct"]
        ]
        st.metric(
            "Mock-exam score",
            f"{len(correct)} / {len(selected)}",
            f"{round(100 * len(correct) / len(selected))}%",
        )
        performance: list[dict[str, Any]] = []
        for domain in DOMAIN_NAMES:
            pool = [question for question in selected if question.get("domain") == domain]
            if pool:
                right = sum(
                    session["answers"].get(index) == question["correct"]
                    for index, question in enumerate(selected)
                    if question.get("domain") == domain
                )
                performance.append(
                    {
                        "Area": f"Domain {domain}: {DOMAIN_NAMES[domain]}",
                        "Score": f"{right}/{len(pool)}",
                        "Percent": round(100 * right / len(pool)),
                    }
                )
        st.subheader("Performance by domain")
        st.dataframe(performance, hide_index=True, width="stretch")
        section_performance: list[dict[str, Any]] = []
        for section in sorted({q["section"] for q in selected}, key=section_sort_key):
            indexes = [i for i, q in enumerate(selected) if q["section"] == section]
            right = sum(
                session["answers"].get(i) == selected[i]["correct"] for i in indexes
            )
            section_performance.append(
                {"Section": section, "Score": f"{right}/{len(indexes)}"}
            )
        st.subheader("Performance by section")
        st.dataframe(section_performance, hide_index=True, width="stretch")
        actions = st.columns(2)
        if actions[0].button(
            "Review incorrect answers",
            disabled=not incorrect,
            width="stretch",
        ):
            session["review_missed"] = not session.get("review_missed", False)
            st.rerun()
        if actions[1].button("Start a new mock exam", type="primary", width="stretch"):
            clear_mock_widgets()
            st.session_state["mock_session"] = {"status": "setup"}
            st.rerun()
        shown = [
            index
            for index, question in enumerate(selected)
            if not session.get("review_missed")
            or session["answers"].get(index) != question["correct"]
        ]
    else:
        answers = {
            index: st.session_state.get(f"mock_answer_{index}")
            for index in range(len(selected))
        }
        answered = sum(bool(answer) for answer in answers.values())
        st.progress(
            answered / len(selected),
            text=f"Answered {answered} of {len(selected)}",
        )
        shown = range(len(selected))

    for index in shown:
        question = selected[index]
        with st.container(border=True):
            st.markdown(f"**Question {index + 1} of {len(selected)}**")
            literal(question["question"])
            st.caption(
                f"{question['section']} · Domain {question.get('domain', '—')}"
            )
            st.radio(
                f"Mock exam answer for question {index + 1}",
                session["choice_orders"][question["_id"]],
                format_func=lambda letter, options=question["options"]: (
                    f"{letter}) {options[letter]}"
                ),
                index=None,
                key=f"mock_answer_{index}",
                disabled=submitted,
            )
            if submitted:
                selected_letter = session["answers"].get(index)
                correct_letter = question["correct"]
                st.markdown(
                    "**Correct**"
                    if selected_letter == correct_letter
                    else "**Incorrect**"
                )
                st.markdown("**Your selected answer**")
                literal(
                    "Not answered"
                    if selected_letter is None
                    else f"{selected_letter}) {question['options'][selected_letter]}"
                )
                st.markdown("**Correct answer**")
                literal(f"{correct_letter}) {question['options'][correct_letter]}")
                st.markdown("**Explanation**")
                literal(question["explanation"] or "No explanation was captured.")

    if not submitted:
        answers = {
            index: st.session_state.get(f"mock_answer_{index}")
            for index in range(len(selected))
        }
        answered = sum(bool(answer) for answer in answers.values())
        if expired:
            st.warning("The timer has expired. Submit to score the answers completed so far.")
        if st.button(
            "Submit mock exam",
            type="primary",
            disabled=answered != len(selected) and not expired,
            width="stretch",
        ):
            session["answers"] = dict(answers)
            session["status"] = "submitted"
            correct_ids = [
                question["_id"]
                for index, question in enumerate(selected)
                if answers.get(index) == question["correct"]
            ]
            incorrect_ids = [
                question["_id"]
                for index, question in enumerate(selected)
                if answers.get(index) != question["correct"]
            ]
            sources = {
                question["_id"]: (question["_quiz_id"], question["section"])
                for question in selected
            }
            store.record_quiz_attempt(
                quiz_id=f"mock:{int(session['started_at'])}",
                section="Mixed sections",
                topic="Mock exam",
                score=len(correct_ids),
                total=len(selected),
                incorrect_ids=incorrect_ids,
                correct_ids=correct_ids,
                mode="mock",
                question_sources=sources,
            )
            st.rerun()


def render_mistake_review(
    store: ProgressStore,
    question_lookup: Mapping[str, Mapping[str, Any]],
) -> None:
    st.header("Review mistakes")
    mistakes = [
        record for record in store.unresolved_mistakes()
        if record["question_id"] in question_lookup
    ]
    if not mistakes:
        st.info(
            "No unresolved quiz mistakes. Incorrect answers from quizzes and mock exams "
            "will appear here automatically."
        )
        return
    st.caption(f"{len(mistakes)} questions to revisit")
    current_id = st.session_state.get("mistake_question_id")
    ids = [record["question_id"] for record in mistakes]
    if current_id not in ids:
        current_id = ids[0]
        st.session_state["mistake_question_id"] = current_id
        st.session_state.pop("mistake_answer", None)
        st.session_state.pop("mistake_result", None)
    question = question_lookup[current_id]
    result = st.session_state.get("mistake_result")
    with st.container(border=True):
        st.caption(f"{question['section']} · {question['topic']}")
        literal(question["question"])
        selected = st.radio(
            "Answer for mistake-review question",
            list(question["options"]),
            format_func=lambda letter: f"{letter}) {question['options'][letter]}",
            index=None,
            key="mistake_answer",
            disabled=result is not None,
        )
        if result is None:
            if st.button("Check answer", type="primary", disabled=selected is None):
                correct = selected == question["correct"]
                store.record_mistake_review(current_id, correct)
                st.session_state["mistake_result"] = correct
                result = correct
        if result is not None:
            st.markdown("**Correct**" if result else "**Incorrect — keep this in your review queue**")
            st.markdown("**Correct answer**")
            literal(f"{question['correct']}) {question['options'][question['correct']]}")
            st.markdown("**Explanation**")
            literal(question["explanation"] or "No explanation was captured.")
            if st.button("Next review question", width="stretch"):
                remaining = [value for value in ids if value != current_id]
                st.session_state["mistake_question_id"] = remaining[0] if remaining else None
                st.session_state.pop("mistake_answer", None)
                st.session_state.pop("mistake_result", None)
                st.rerun()


def app() -> None:
    """Load dependencies and render the selected application view."""
    header, reload_column = st.columns((6, 1))
    header.title("AWS Data Engineer Study")
    header.caption("DEA-C01 · private progress for every learner")
    if reload_column.button("Reload data", width="stretch"):
        load_data.clear()
        st.rerun()

    try:
        version = data_file_version(DATA_PATH)
        runtime = load_data(str(DATA_PATH), version)
    except StudyDataError as exc:
        st.error(str(exc))
        st.info("Fix or rebuild the generated file, then select Reload data.")
        st.stop()

    data = runtime.data
    cards = runtime.cards
    quizzes = runtime.quizzes
    questions = runtime.questions
    question_lookup = runtime.question_lookup
    data_fingerprint = runtime.fingerprint

    config = configuration()
    user = require_user(config)
    try:
        store = open_progress_store(
            str(PROGRESS_PATH),
            config["database_url"],
            user.user_id,
        )
    except Exception as exc:
        location = "Supabase Postgres" if config["database_url"] else str(PROGRESS_PATH)
        st.error(f"Progress storage could not be opened at {location}: {exc}")
        st.info("Check the deployment secrets or local directory permissions, then retry.")
        st.stop()

    session_scope = make_session_scope(user, store)
    ensure_session_scope(st.session_state, session_scope)

    if st.session_state.pop("_session_reset_requested", False):
        clear_working_session_state(st.session_state)
        st.session_state["_session_scope"] = session_scope
        st.session_state["_session_initialized"] = True
        st.session_state["_session_started_fresh"] = True

    if not st.session_state.get("_session_initialized"):
        saved = store.load_active_session()
        if saved and saved["data_fingerprint"] == data_fingerprint:
            restored = restore_session_state(
                st.session_state,
                saved["payload"],
                question_lookup,
            )
            if restored:
                st.session_state["_session_restored_at"] = saved["updated_at"]
                mark_session_payload_saved(
                    st.session_state,
                    saved["payload"],
                    data_fingerprint,
                    session_scope,
                )
        elif saved:
            store.clear_active_session()
            st.session_state["_session_data_changed"] = True
        st.session_state["_session_initialized"] = True

    identity_columns = st.columns((4, 1, 1))
    identity_columns[0].caption(
        f"{user.display_name} · "
        + ("Supabase cloud sync" if store.backend == "postgres" else "local SQLite")
    )
    identity_columns[1].button(
        "Start fresh",
        help="Discard the active working session but keep ratings, bookmarks, and attempts.",
        width="stretch",
        on_click=start_fresh_session,
        args=(store, session_scope),
    )
    if user.authentication_enabled:
        identity_columns[2].button("Sign out", width="stretch", on_click=st.logout)
    if store.backend == "sqlite":
        st.warning(
            "Local SQLite mode: progress is stored only on this machine. "
            "If this app is running on Streamlit Community Cloud, do not begin "
            "studying until Google sign-in and Supabase are configured."
        )
    if st.session_state.get("_session_restored_at"):
        restored_at = from_iso(st.session_state["_session_restored_at"])
        when = restored_at.astimezone().strftime("%b %-d, %-I:%M %p") if restored_at else "earlier"
        st.info(f"Continued your saved session from {when}.")
        st.session_state.pop("_session_restored_at", None)
    elif st.session_state.pop("_session_data_changed", False):
        st.info("The study data changed, so the previous working session was safely discarded.")
    elif st.session_state.pop("_session_started_fresh", False):
        st.info("Started a fresh working session. Your study progress was kept.")

    if st.session_state.get("main_nav") not in NAV_ITEMS:
        st.session_state["main_nav"] = "Dashboard"
    navigation = st.radio(
        "Primary navigation",
        NAV_ITEMS,
        horizontal=True,
        key="main_nav",
        label_visibility="collapsed",
    )
    st.divider()

    if navigation == "Dashboard":
        render_dashboard(store, cards)
    elif navigation == "Flashcards":
        render_flashcards(data, store, cards)
    elif navigation == "Quizzes":
        render_quizzes(data, store, quizzes)
    elif navigation == "Mock Exam":
        render_mock_exam(store, questions)
    else:
        render_mistake_review(store, question_lookup)

    st.divider()
    st.caption(
        f"{len(cards):,} flashcards · {len(quizzes):,} quizzes · "
        f"{sum(len(quiz['mc']) for quiz in quizzes):,} multiple-choice questions"
    )
    try:
        persist_session_if_changed(
            store,
            st.session_state,
            data_fingerprint,
            session_scope,
        )
    except Exception as exc:
        st.warning(f"Your working session could not be saved: {exc}")


if __name__ == "__main__":
    app()
