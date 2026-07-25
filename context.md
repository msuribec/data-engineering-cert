# Project Context — AWS Certified Data Engineer (DEA-C01)

## Goal
Prepare for and pass the **AWS Certified Data Engineer — Associate (DEA-C01)** exam. This folder holds the raw course material, the study material generated from it, and a small study app for reviewing flashcards and quizzes.

The exam covers four domains: **Data Ingestion & Transformation (34%)**, **Data Store Management (26%)**, **Data Operations & Support (22%)**, and **Data Security & Governance (18%)**. The material is organized by AWS service area across 14 sections (Sections 2–15, mirroring the Udemy course numbering).

## Folder structure at a glance
```
data-engineering-cert/
├── AWS_DEA_Study.html                   ← self-contained offline study app
├── app.py                               ← Streamlit study app
├── study_core.py                        ← validation, IDs, filters, queues, quiz helpers
├── progress_store.py                    ← user-scoped SQLite/Postgres progress
├── session_persistence.py               ← safe working-session snapshots
├── context.md                           ← this file
├── README.md                            ← quick app instructions
├── deploy_steps.md                      ← free-tier deployment guide
├── requirements.txt                    ← runtime and AppTest dependencies
├── tests/                               ← unit tests + Streamlit AppTest flows
├── data/
│   └── study_data.json                  ← parsed data used by app.py
├── .study_progress/                     ← generated local progress (Git-ignored)
├── src/
│   ├── build_data.py                    ← rebuilds data/study_data.json
│   ├── study_app.py                     ← older copy of the Streamlit app
│   └── README.md                        ← older copy of the app instructions
└── Study Materials/
    ├── AWSCertifiedDataEngineerSlides.pdf
    ├── Flashcards/
    │   ├── Flashcards_QA.csv
    │   ├── Flashcards_AWS_Services.csv
    │   └── Flashcards_Terms.csv
    ├── Transcripts/                     ← 302 lecture transcripts, 14 sections
    └── Study sheets and Quiz/           ← 182 study sheets + 182 quizzes, 14 sections
```

There is also a root-level `Flashcards_Terms.csv`, which duplicates the current file under `Study Materials/Flashcards/`. The build scripts use the copy inside `Study Materials/Flashcards/`.

## The source materials (inputs)

**Study Materials/AWSCertifiedDataEngineerSlides.pdf** is the slide deck from the Udemy course — the primary visual reference.

**Study Materials/Transcripts/** holds the text transcripts of every video lesson, grouped into 14 section folders (Section 2 – Section 15), 302 `.txt` files in total. File names follow the lecture order within each section (e.g. `4. Amazon S3.txt`, `5. Amazon S3 - Hands On.txt`). These are the raw source the study material was derived from.

## The study materials (created for studying)

**Study Materials/Study sheets and Quiz/** contains, for each of the 14 sections, one **study sheet** and one **quiz** per lesson topic — 182 of each (`.docx`). They are named like `01 - Amazon S3 Fundamentals - Study Sheet.docx` and `01 - Amazon S3 Fundamentals - Quiz.docx`.

Each **study sheet** has a fixed structure: lesson summary, key AWS services, important exam facts, common traps, a *Terms to Memorize* table (Term / Definition), *Easy to Mix Up* items, docs to verify, exam domain mapping, and hardest concepts.

Each **quiz** has a fixed structure: Part A multiple choice (5 scenario-based questions, one correct answer each), Part B short answer, and an answer key with the correct letter plus an explanation of why it's right and why the others are wrong.

**Study Materials/Flashcards/** contains the three flashcard CSVs. They all share the columns `Front`, `Back`, and `Tags`; `Tags` encodes the section (e.g. `DEA-C01 Section-3-Storage`) so cards can be filtered:
- `Flashcards_QA.csv` — 2,563 question → answer cards.
- `Flashcards_AWS_Services.csv` — 292 cards, one per service, describing its purpose and when to use it.
- `Flashcards_Terms.csv` — 614 cards, key term → definition, extracted from the study sheets' *Terms to Memorize* tables.

## The Study App

The root-level app files turn the materials above into an interactive review tool. The Streamlit app offers a progress dashboard, three flashcard decks with filtering and spaced repetition, topic quizzes, active short-answer self-checks, automatic mistake review, and mixed-topic mock exams. Progress is always separate from `data/study_data.json`: local development uses `.study_progress/progress.db`, while the deployed multi-user app uses a Supabase Postgres database.

There are two front ends running off the same data:

**AWS_DEA_Study.html** — a single self-contained file. Double-click to open in any browser; no install, works offline because all data is embedded in the file. This is the easiest way to study.

**app.py** — the current Streamlit version (`pip install -r requirements.txt` then `streamlit run app.py`). It reads `data/study_data.json`. With no secrets it runs fully locally using SQLite. In the free-tier deployment, Streamlit Community Cloud hosts the private app, Google OIDC signs learners in, and Supabase stores their progress and active working sessions.

### Persistence and identity

- Local mode requires no account or network access and stores one learner under the `local` user ID.
- Cloud mode derives a stable opaque user ID from the Google identity token's issuer and subject. Email is used only by the optional access allowlist and is not stored in progress tables.
- Every progress query is scoped by user ID. Ratings, schedules, bookmarks, quiz history, mistakes, short-answer ratings, and the active working session cannot leak between learners.
- One working session is saved per learner. It includes navigation, flashcard queue and position, quiz drafts/review state, mock-exam ordering and timer start, and mistake-review state.
- Session snapshots store stable IDs rather than copies of study questions. If `data/study_data.json` changes incompatibly, only the working snapshot is discarded; long-term progress remains.
- **Start fresh** clears the active working session while keeping progress. **Reset my progress** deletes only the signed-in learner's progress and saved session.
- The cloud database URL and Google client secret are server-side Streamlit secrets. They must never be committed.

### Code / data files
- `data/study_data.json` — the parsed data used by the Streamlit app and used to produce the HTML app's embedded dataset: all flashcards, plus all quizzes with their questions, options, correct answers, and explanations.
- `src/build_data.py` — reads the three CSVs in `Study Materials/Flashcards/` and all 182 quiz `.docx` files, then writes `data/study_data.json`. It extracts multiple-choice questions, options, correct answers, explanations, short-answer questions, and the explicit primary exam-domain mapping from each matching study sheet.
- There is currently no separate terms-generation script under `src/`. `Study Materials/Flashcards/Flashcards_Terms.csv` is an input to `src/build_data.py`.
- `src/study_app.py` and `src/README.md` — older copies retained in `src/`; use the root-level `app.py` and `README.md` for the current layout.
- `study_core.py` — reusable data validation, natural section ordering, stable content IDs, search/filtering, persistent review queues, and quiz-state helpers.
- `progress_store.py` — initializes/upgrades the shared schema and stores user-scoped flashcard schedules, bookmarks, quiz attempts, mistakes, short-answer self-ratings, and active sessions. It supports SQLite locally and Postgres in deployment; existing version-one local SQLite data is migrated without deleting the legacy tables.
- `session_persistence.py` — captures only the durable, safe subset of Streamlit session state and rehydrates question IDs against the current generated data.
- `.streamlit/config.toml` — forces the requested light theme.
- `.streamlit/secrets.example.toml` — credential-free example for Google OIDC, Supabase, and the optional email allowlist.
- `deploy_steps.md` — exact setup and verification steps for the free-tier multi-user architecture.
- `tests/` — standard-library unit tests and Streamlit `AppTest` end-to-end flows.

## How it all fits together (data flow)
```
Study Materials/Transcripts + Study Materials/AWSCertifiedDataEngineerSlides.pdf
        │  (studied / authored into)
        ▼
Study Materials/Study sheets and Quiz/       Study Materials/Flashcards/
        │                                    (QA and AWS Services)
        │                                     │
        │                                     │
Study Materials/Flashcards/                   │
Flashcards_Terms.csv ─────────────────────────┤
                              ▼               ▼
                    src/build_data.py  →  data/study_data.json
                                              │
                          ┌───────────────────┴───────────────────┐
                          ▼                                        ▼
                  AWS_DEA_Study.html                              app.py
                  (embedded data)                             (reads JSON)
                                                                   │
                                        local: SQLite ◄────────────┤
                                        cloud: Google OIDC         │
                                               + Supabase Postgres ◄┘
```

## Regenerating everything
If the study sheets or quizzes change, from the project root:
```
pip install python-docx
python src/build_data.py      # rebuild data/study_data.json
```
Then re-embed `study_data.json` into `AWS_DEA_Study.html` if the offline HTML copy also needs updating (the HTML holds a baked-in copy inside a `<script id="study-data">` tag). The Streamlit app detects the new JSON modification time on its next rerun; its **Reload data** control triggers that rerun without an app restart.

## Deployment

The intended small-group deployment is a private Streamlit Community Cloud app with Google OIDC and a Supabase Free Postgres project. Keep the repository private and do not upload the original course materials. Follow `deploy_steps.md` for the exact repository, Supabase, Google OAuth, Streamlit secrets, sharing, and verification workflow.

## Current totals
2,563 Q/A cards · 292 service cards · 614 term cards · 182 quizzes (910 multiple-choice + 910 short-answer questions) across 14 sections.
