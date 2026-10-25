# Meesho Shipping Checker SaaS — MVP scaffold

This is a deployable Flask web app scaffold with user registration/login, admin user/plan controls, upload-job records, processing history, CSV/JSON export, and a free-test plan. It includes the current desktop Playwright app as `desktop_main_reference.py` for reference.

## Important: what is and is not live
- **Working in this MVP:** registration/login, password hashing, per-user job ownership, image uploads (max 100/job), history, admin plan flags, CSV/JSON export, health endpoint.
- **Not yet connected:** real Meesho cloud browser automation, live shipping charge extraction, paid billing/webhooks. New jobs are deliberately marked `needs_setup`; they are not falsely reported as checked.
- A cloud browser cannot simply reuse the desktop app's `127.0.0.1:9222` Chrome. Each user needs an isolated persistent browser context, secure session storage, OTP/CAPTCHA/manual sign-in path, worker queue, cleanup, and a verified current Meesho selector flow. Do not collect users' Meesho passwords or share browser profiles between users.

## Run locally (free test)
1. Install Python 3.11+.
2. In this folder: `python -m venv .venv`
3. Activate the venv, then `pip install -r requirements.txt`.
4. Copy `.env.example` to `.env`, set a random `SECRET_KEY`, `ADMIN_EMAIL`, and strong `ADMIN_PASSWORD`.
5. Load environment variables (PowerShell: `$env:SECRET_KEY='...'; $env:ADMIN_EMAIL='admin@example.com'; $env:ADMIN_PASSWORD='...'`) then run `python app.py`.
6. Open `http://127.0.0.1:5000`.

For an easy local run, use `python -m pip install python-dotenv` and load `.env` through your shell or add `from dotenv import load_dotenv; load_dotenv()` before config initialization in `app.py`.

## Deploy on a free test host
A Flask-compatible host that runs a web service can deploy this repository. Set build command `pip install -r requirements.txt`; start command `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 120`. Add environment variables `SECRET_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`. Use a persistent disk for SQLite only during testing; ephemeral filesystems lose database/uploads on restart. For multi-user production, use managed PostgreSQL + private object storage and a worker service.

## Before public launch
1. Add CSRF protection, rate limiting, email verification, password reset, secure cookie settings, and audit logs.
2. Use PostgreSQL, object storage with per-user access rules, and background task queue (Redis/Celery or equivalent).
3. Implement an isolated Playwright worker per user with persistent context encrypted at rest; let users sign in through the browser themselves. Never store Meesho passwords. Keep OTP/CAPTCHA user-controlled and obey applicable site terms.
4. Reuse the selectors from the desktop reference only after verifying current supplier UI. `#meesho_price` and `[data-testid="change-front-image"]` can change. Wait for an actual page update and validate charge labels/amounts; mark ambiguous values `Needs review`.
5. Add a payment gateway only after merchant account setup; verify signed webhooks server-side. Current billing page is informational and does not charge users.
6. Add per-user upload quotas, image retention/deletion policy, malware/content checks, worker timeouts, and monitoring.

## Existing desktop automation
`desktop_main_reference.py` contains the desktop Playwright workflow. Its CDP URL points to a local browser and must **not** be used unchanged on a cloud server. The cloud worker needs its own browser lifecycle and authenticated user context.
