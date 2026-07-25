# Source utilities

Run commands from the project root.

- `build_data.py` regenerates `data/study_data.json` from the three flashcard
  CSVs and the quiz/study-sheet documents under `Study Materials/`.
- `study_app.py` is a historical copy of the old Streamlit interface. The
  supported entry point is the root-level `app.py`.

```bash
source .venv/bin/activate
python src/build_data.py
streamlit run app.py
```

The current Streamlit app detects the rebuilt JSON on its next rerun. Select
**Reload data** to trigger that rerun immediately; an app restart is unnecessary.
