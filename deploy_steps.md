# Free-tier deployment guide

This guide deploys the study app for roughly 6–10 learners using:

- A **private GitHub repository** for the generated app code and data
- **Streamlit Community Cloud** for hosting
- **Google OpenID Connect (OIDC)** for learner identity
- **Supabase Free Postgres** for durable, per-user progress and session storage

Within the providers' free-tier limits, this architecture can cost $0. Free-tier
limits and inactivity policies can change, so review the linked provider pages
before deployment. The app remains fully offline when run locally without
secrets; the deployed version uses only Google sign-in and Supabase persistence.

## 1. Test the app locally

From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m py_compile \
  app.py study_core.py progress_store.py session_persistence.py src/build_data.py
streamlit run app.py
```

Local progress is written to `.study_progress/progress.db`. This file, real
Streamlit secrets, and the original course materials are Git-ignored.

## 2. Put only deployable files in a private GitHub repository

Create an empty **private** GitHub repository. Before committing, inspect the
local repository:

```bash
git status --short
git check-ignore -v \
  .streamlit/secrets.toml \
  .study_progress/progress.db \
  "Study Materials/AWSCertifiedDataEngineerSlides.pdf" \
  AWS_DEA_Study.html
```

The command should show that those private/generated files are ignored. Commit
the app, tests, generated `data/study_data.json`, and documentation. Do **not**
commit:

- `.streamlit/secrets.toml`
- `.study_progress/`
- `Study Materials/`
- `AWS_DEA_Study.html`
- Any Google or Supabase credential

Review the exact staged file list before pushing:

```bash
git diff --cached --name-only
git diff --cached
```

Then push the private repository to GitHub. Streamlit Community Cloud deploys
from this repository and uses `app.py` as its entry point.

## 3. Create the Supabase Free database

1. Create a project from the [Supabase dashboard](https://supabase.com/dashboard)
   using the **Free** plan.
2. Choose a region close to the learners and save the generated database
   password in a password manager.
3. In the project, open **Connect** and select the **Session pooler** connection
   string.
4. Confirm the connection hostname ends in `pooler.supabase.com` and the port is
   **5432**.

Use the Session pooler because Streamlit is a long-running application and the
pooler supports IPv4. Do not use the direct IPv6-only URL or the transaction
pooler on port 6543. Supabase documents the connection choices in
[Connecting to Postgres](https://supabase.com/docs/guides/database/connecting-to-postgres).

The URL has this general shape:

```text
postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require
```

If the password contains reserved URL characters, URL-encode it:

```bash
python -c "from urllib.parse import quote; print(quote(input('Database password: '), safe=''))"
```

Keep the full database URL private. The application creates and upgrades its
tables automatically on the first successful authenticated launch; no manual
SQL is required.

## 4. Reserve the Streamlit app URL

1. Sign in at [Streamlit Community Cloud](https://share.streamlit.io/).
2. Select **Create app** and connect the private GitHub repository.
3. Choose the repository, branch, and `app.py`.
4. Choose the final app subdomain before configuring Google OAuth, for example:

   ```text
   https://dea-study-group.streamlit.app
   ```

5. Deploy once. It is expected to show a sign-in or configuration issue until
   the secrets below are added.

Community Cloud's current private-app sharing behavior is described in
[Share your app](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app).

## 5. Configure Google OAuth/OIDC

Follow Streamlit's
[Google authentication tutorial](https://docs.streamlit.io/develop/tutorials/authentication/google)
and use the final Streamlit URL from step 4:

1. Open the [Google Cloud Console](https://console.cloud.google.com/) and create
   or select a project.
2. Configure **Google Auth Platform** / the OAuth consent screen.
3. Use an external audience in testing mode and add the 6–10 learners as test
   users.
4. Create an OAuth client with application type **Web application**.
5. Add this authorized JavaScript origin:

   ```text
   https://YOUR-APP.streamlit.app
   ```

6. Add this exact authorized redirect URI:

   ```text
   https://YOUR-APP.streamlit.app/oauth2callback
   ```

7. Save the OAuth **client ID** and **client secret** securely.

The hostname, protocol, and `/oauth2callback` path must match exactly. If the
Streamlit subdomain changes, update both Google and Streamlit settings.

## 6. Add Streamlit secrets

Generate a separate cookie secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

In Streamlit Community Cloud, open the app's **Settings → Secrets** and add the
following TOML. Replace every placeholder:

```toml
[auth]
redirect_uri = "https://YOUR-APP.streamlit.app/oauth2callback"
cookie_secret = "YOUR_RANDOM_COOKIE_SECRET"
client_id = "YOUR_GOOGLE_OAUTH_CLIENT_ID"
client_secret = "YOUR_GOOGLE_OAUTH_CLIENT_SECRET"
server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"

[database]
url = "postgresql://postgres.PROJECT_REF:URL_ENCODED_PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require"

[app]
allowed_emails = [
  "learner1@example.com",
  "learner2@example.com",
  "learner3@example.com",
]
```

List all 6–10 learners in `allowed_emails`. Matching is case-insensitive. The
allowlist is defense in depth: private Streamlit sharing controls who can open
the app, and this list controls which signed-in Google identities the app
accepts.

Do not put these real values in `.streamlit/secrets.toml` in the repository.
`.streamlit/secrets.example.toml` is the safe, credential-free reference.

Streamlit's OIDC behavior and secret fields are documented in
[User authentication and information](https://docs.streamlit.io/develop/concepts/connections/authentication).

## 7. Reboot and share the private app

1. Save the Streamlit secrets and reboot the app.
2. In the app's sharing settings, keep the app private.
3. Invite the same 6–10 Google-account email addresses used in the allowlist.
4. Ask each learner to open the app and select **Sign in with Google**.

The app header should show the learner's display name and **Supabase cloud
sync**. If it says **local SQLite**, the database secret was not loaded and the
deployment is not ready for persistent multi-user use.

## 8. Verify account isolation and resume behavior

Complete this acceptance check before sending the app to the full group:

1. Sign in as learner A.
2. Open Flashcards, apply filters, shuffle, move to a later card, and rate one
   card.
3. Start a topic quiz and select two answers without submitting.
4. Close the browser tab, reopen the app, and sign in again. Confirm the quiz
   draft resumes.
5. Select **Start fresh**. Confirm the draft disappears but the card review
   remains on the dashboard.
6. Sign out and sign in as learner B. Confirm learner B starts with an empty
   dashboard and cannot see learner A's draft or progress.
7. Start a timed mock exam, close the tab, and reopen it. Confirm the original
   timer continues rather than restarting.
8. In Supabase's Table Editor, confirm rows appear in the `study_*` tables and
   use opaque 64-character `user_id` values rather than email addresses.

The most important tables are:

- `study_card_progress`
- `study_quiz_attempts`
- `study_mistakes`
- `study_short_answer_reviews`
- `study_active_sessions`

## 9. Publish updates

Push a new commit to the configured GitHub branch. Streamlit Community Cloud
will rebuild the app automatically.

When `data/study_data.json` is regenerated:

```bash
source .venv/bin/activate
python src/build_data.py
python -m unittest discover -s tests -v
```

Commit the rebuilt JSON and push it. An app restart is not required locally;
the JSON modification time invalidates the cache on the next rerun. A cloud
redeploy naturally starts the new code and data. If stable study IDs changed,
the app safely drops incompatible working-session snapshots but keeps long-term
progress.

## Troubleshooting

### “A cloud database is configured without OIDC authentication”

The `[database]` secret loaded but `[auth]` did not. Add the complete `[auth]`
section and reboot the app. The app deliberately refuses to put multiple users
into a shared anonymous account.

### Google reports `redirect_uri_mismatch`

Compare the Google authorized redirect URI and `[auth].redirect_uri` character
for character. Both must be:

```text
https://YOUR-APP.streamlit.app/oauth2callback
```

### An invited learner is “not authorized”

Add that Google account's exact email address to `[app].allowed_emails`, save
the secrets, and reboot. Also confirm the account is a Google OAuth test user
while the consent screen remains in testing mode.

### The database cannot be opened

- Use the Supabase **Session pooler** URL on port **5432**.
- Confirm the database password is URL-encoded.
- Confirm the Supabase project is running rather than paused.
- Confirm `psycopg[binary]` remains in `requirements.txt`.
- Rotate the database password immediately if its URL was ever committed or
  shared publicly.

### The app is slow on its first visit

Free hosting and database projects may sleep or pause after inactivity. Wake or
resume the project from its provider dashboard, then reload the app. Review the
current [Supabase pricing and free-plan details](https://supabase.com/pricing)
before relying on the app for a scheduled group session.

### A deployment accidentally shows “local SQLite”

Do not use that deployment for studying: Community Cloud's local filesystem is
not durable. Recheck `[database].url`, save the secrets, reboot, and verify the
**Supabase cloud sync** label before continuing.
