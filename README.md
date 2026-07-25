# AWS DEA-C01 Study App

A Streamlit application for studying the AWS Certified Data Engineer —
Associate exam. It includes 3,469 flashcards, 182 topic quizzes, short-answer
self-checks, spaced repetition, mistake review, resumable sessions, and
domain-weighted mock exams.

## Persistence modes

The same launch command supports two configurations:

- **Local development:** no credentials required. Progress and the active
  session are stored in `.study_progress/progress.db` with SQLite.
- **Deployed multi-user app:** Google OIDC identifies each learner and Supabase
  Postgres stores user-scoped progress and active sessions. No progress is
  written to Streamlit Community Cloud's temporary filesystem.

Every database query is scoped with an opaque ID derived from the Google
identity token's issuer and subject. Email addresses are used only for the
optional deployment allowlist and are not stored in progress tables.

## Local setup and launch

Python 3.11 or newer is recommended.

```bash
cd /Users/mariasofiauribe/Projects/02_Active/data-engineering-cert
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
```

Without `.streamlit/secrets.toml` or a database environment variable, the app
automatically uses the local learner and SQLite. Existing version-one local
progress is migrated to the user-scoped schema on first launch.

For the complete free-tier Google, Supabase, and Streamlit configuration, follow
[deploy_steps.md](deploy_steps.md).

## What is resumed

The app automatically restores one latest working session per learner:

- Current navigation page
- Flashcard deck, view, filters, shuffled queue, position, and visible side
- Quiz topic, draft selections, submitted review state, and short-answer drafts
- Mock-exam question order, answer-choice order, selections, review state, and
  original timer start
- Mistake-review question, selection, and revealed result

Snapshots contain stable card/question IDs rather than duplicated study content.
If `data/study_data.json` changes, an incompatible working snapshot is discarded
safely while long-term progress remains intact. **Start fresh** clears only the
working session; **Reset my progress** deletes only the signed-in learner's data.

## Rebuild study data

The app reads `data/study_data.json`. Regenerate it locally from the source
materials and explicit study-sheet domain mappings:

```bash
source .venv/bin/activate
python src/build_data.py
```

An app restart is unnecessary. The data modification time invalidates the cache
on the next rerun; **Reload data** triggers that rerun immediately. Source
materials under `Study Materials/` are deliberately Git-ignored and must not be
uploaded to the deployment repository.

## Tests

Tests use standard `unittest`, temporary SQLite files, and Streamlit `AppTest`:

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
python -m py_compile \
  app.py study_core.py progress_store.py session_persistence.py src/build_data.py
```

The suite covers user isolation, schema migration, session capture and resume,
flashcard queues, quiz locking, mock exams, malformed data, and empty progress.
A live Supabase connection is configured only at deployment and is not required
to run local tests.

## Spaced repetition

- **Again:** due in 10 minutes and reduces ease.
- **Hard:** starts at 1 day and grows slowly.
- **Good:** starts at 1 day and multiplies by current ease.
- **Easy:** starts at 4 days, receives an extra multiplier, and raises ease.

Intervals grow with successful reviews; ease never falls below 1.3.

## Keyboard navigation

Streamlit does not expose a reliable native, focus-safe keyboard-event API for
Space/arrow/number shortcuts. The app avoids a fragile custom component; all
visible controls remain reachable by standard keyboard focus and activation.
