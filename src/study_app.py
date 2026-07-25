#!/usr/bin/env python3
"""Historical Streamlit implementation.

Use the supported root entry point instead:
    streamlit run app.py

The current app reads data/study_data.json. Rebuild it from the project root:
    python src/build_data.py
"""
import json
import os
import random

import streamlit as st

DATA_PATH = os.path.join(os.path.dirname(__file__), "study_data.json")

st.set_page_config(page_title="AWS DEA-C01 Study", page_icon="📚", layout="centered")


@st.cache_data
def load_data():
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)


try:
    DATA = load_data()
except FileNotFoundError:
    st.error("study_data.json not found. Run build_data.py first (see the docstring at the top of this file).")
    st.stop()

# ---------- styling ----------
st.markdown(
    """
    <style>
    .card {border:1px solid #d0d7de;border-radius:14px;padding:28px 26px;
           background:#fff;box-shadow:0 2px 8px rgba(0,0,0,.06);min-height:150px;
           font-size:1.15rem;line-height:1.5;}
    .card .label {font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;
           color:#8a94a6;margin-bottom:10px;}
    .tag {display:inline-block;background:#eef2ff;color:#4f46e5;border-radius:999px;
          padding:2px 10px;font-size:.72rem;margin:2px 4px 2px 0;}
    .correct {color:#0f7b3f;font-weight:600;}
    .wrong {color:#c0392b;font-weight:600;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📚 AWS Data Engineer — Study")
st.caption("DEA-C01 · flashcards and quizzes built from your course materials")

mode = st.sidebar.radio("Mode", ["Flashcards", "Quizzes"])


# ============================ FLASHCARDS ============================
def flashcards():
    deck_name = st.sidebar.selectbox(
        "Deck", ["Q/A flashcards", "AWS service flashcards", "Key terms"])
    deck_map = {
        "Q/A flashcards": "flashcards_qa",
        "AWS service flashcards": "flashcards_services",
        "Key terms": "flashcards_terms",
    }
    cards_all = DATA.get(deck_map[deck_name], [])

    sections = sorted({c["section"] for c in cards_all if c["section"]})
    chosen = st.sidebar.multiselect("Filter by section", sections)
    search = st.sidebar.text_input("Search text")

    cards = cards_all
    if chosen:
        cards = [c for c in cards if c["section"] in chosen]
    if search:
        s = search.lower()
        cards = [c for c in cards if s in c["front"].lower() or s in c["back"].lower()]

    if not cards:
        st.info("No cards match your filters.")
        return

    # session state
    if "fc_idx" not in st.session_state or st.session_state.get("fc_deck") != deck_name or \
            st.session_state.get("fc_n") != len(cards):
        st.session_state.fc_idx = 0
        st.session_state.fc_show = False
        st.session_state.fc_deck = deck_name
        st.session_state.fc_n = len(cards)

    st.sidebar.markdown(f"**{len(cards)}** cards")
    if st.sidebar.button("🔀 Shuffle"):
        random.shuffle(cards)
        st.session_state.fc_idx = 0
        st.session_state.fc_show = False

    i = st.session_state.fc_idx % len(cards)
    card = cards[i]

    st.progress((i + 1) / len(cards), text=f"Card {i + 1} of {len(cards)}")

    side_label = "Answer" if st.session_state.fc_show else "Front"
    body = card["back"] if st.session_state.fc_show else card["front"]
    body = body.replace(" | ", "<br>• ")
    st.markdown(f'<div class="card"><div class="label">{side_label}</div>{body}</div>',
                unsafe_allow_html=True)

    if card.get("section"):
        st.markdown(f'<span class="tag">{card["section"]}</span>', unsafe_allow_html=True)

    c1, c2, c3 = st.columns(3)
    if c1.button("◀ Prev", use_container_width=True):
        st.session_state.fc_idx = (i - 1) % len(cards)
        st.session_state.fc_show = False
        st.rerun()
    if c2.button("🔁 Flip", use_container_width=True, type="primary"):
        st.session_state.fc_show = not st.session_state.fc_show
        st.rerun()
    if c3.button("Next ▶", use_container_width=True):
        st.session_state.fc_idx = (i + 1) % len(cards)
        st.session_state.fc_show = False
        st.rerun()


# ============================ QUIZZES ============================
def quizzes():
    quizzes_all = DATA["quizzes"]
    sections = DATA["sections"]
    section = st.sidebar.selectbox("Section", sections)
    topics = [q for q in quizzes_all if q["section"] == section]
    topic_names = [q["topic"] for q in topics]
    tname = st.sidebar.selectbox("Topic", topic_names)
    quiz = next(q for q in topics if q["topic"] == tname)

    key = f"{section}|{tname}"
    if st.session_state.get("quiz_key") != key:
        st.session_state.quiz_key = key
        st.session_state.answers = {}
        st.session_state.submitted = False

    st.subheader(quiz["title"])
    st.caption(f"{section} · {len(quiz['mc'])} multiple choice · {len(quiz['short'])} short answer")

    if not quiz["mc"]:
        st.info("This quiz has no multiple-choice questions.")
    for qi, q in enumerate(quiz["mc"]):
        st.markdown(f"**Q{qi + 1}. {q['question']}**")
        letters = list(q["options"].keys())
        choice = st.radio(
            f"q{qi}",
            letters,
            format_func=lambda L, o=q["options"]: f"{L}) {o[L]}",
            index=None,
            key=f"radio_{key}_{qi}",
            label_visibility="collapsed",
        )
        st.session_state.answers[qi] = choice
        if st.session_state.submitted:
            correct = q["correct"]
            if choice == correct:
                st.markdown(f'<span class="correct">✓ Correct</span>', unsafe_allow_html=True)
            else:
                picked = f"You chose {choice}. " if choice else "Not answered. "
                st.markdown(
                    f'<span class="wrong">✗ {picked}Correct answer: {correct}) {q["options"][correct]}</span>',
                    unsafe_allow_html=True,
                )
            if q["explanation"]:
                st.caption(q["explanation"])
        st.divider()

    if st.button("Submit quiz", type="primary"):
        st.session_state.submitted = True
        st.rerun()

    if st.session_state.submitted and quiz["mc"]:
        score = sum(1 for qi, q in enumerate(quiz["mc"])
                    if st.session_state.answers.get(qi) == q["correct"])
        total = len(quiz["mc"])
        pct = round(100 * score / total)
        st.success(f"Score: {score} / {total}  ({pct}%)")

    if quiz["short"]:
        st.markdown("### Short answer (self-check)")
        for si, s in enumerate(quiz["short"]):
            with st.expander(f"{si + 1}. {s['question']}"):
                st.write(s["answer"] or "_No model answer captured._")


if mode == "Flashcards":
    flashcards()
else:
    quizzes()

st.sidebar.markdown("---")
st.sidebar.caption(
    f"{len(DATA['flashcards_qa'])} Q/A cards · {len(DATA['flashcards_services'])} service cards · "
    f"{len(DATA.get('flashcards_terms', []))} term cards · {len(DATA['quizzes'])} quizzes"
)
