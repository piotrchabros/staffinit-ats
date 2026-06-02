# Deploying StaffInit ATS to Railway

This app runs as **two services from one repo** (a web server and a background
scoring worker) plus a **managed Postgres**. Both services build from the same
`Dockerfile`; they differ only in their start command.

```
Postgres (managed, EU region, UTF-8)
   ▲                 ▲
   │ DATABASE_URL    │
 Web service       Worker service
 gunicorn          procrastinate worker
```

## 1. Project + Postgres

1. **New Project → Deploy from GitHub repo** → select this repo. Railway detects
   the `Dockerfile` and `railway.json`.
2. **New → Database → PostgreSQL.** Railway Postgres is UTF-8 by default (required:
   CVs and AI rationales contain non-ASCII).
3. Set the **region to EU** (e.g. EU West / Amsterdam) for Postgres and both
   services — candidate CVs are PII (GDPR). *(Region selection is a paid-plan
   feature.)*

## 2. Web service

- It's the service created on first deploy. Its start command comes from
  `railway.json` (gunicorn).
- **Settings → Pre-Deploy Command:** `uv run python manage.py migrate --noinput`
  (runs migrations once, before the new version serves). *Set this on the WEB
  service only — not the worker — to avoid concurrent migrations.*
- **Settings → Networking:** generate a public domain.
- Attach a **Volume** mounted at `/app/media` so uploaded CV files survive
  redeploys (Railway's filesystem is otherwise ephemeral). The worker does NOT
  need this — scoring reads `parsed_text` from the DB, not the file.

## 3. Worker service

- **New → GitHub Repo → (same repo).** Same image builds.
- **Settings → Custom Start Command:**
  `uv run python manage.py procrastinate worker --concurrency 4`
  (`--concurrency` bounds parallel Claude calls; raise to match your Anthropic tier).
- No volume, no public domain, no pre-deploy command needed.

## 4. Environment variables (set on BOTH services)

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
DJANGO_SECRET_KEY=<long random string>
DJANGO_DEBUG=false
ANTHROPIC_API_KEY=<your key>
```
`ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` are wired automatically from Railway's
`RAILWAY_PUBLIC_DOMAIN`. Optional overrides: `DJANGO_ALLOWED_HOSTS`,
`DJANGO_CSRF_TRUSTED_ORIGINS` (comma-separated), `SCORING_MODEL`.

## 5. First-run setup (once)

In the web service's shell (Railway → service → Command/Shell):
```
uv run python manage.py createsuperuser
uv run python manage.py seed_rubric        # or build the real rubric in /admin
```

## Notes

- **Static files** are baked into the image at build (`collectstatic`) and served
  by WhiteNoise — nothing to configure.
- **GDPR:** EU hosting covers data-at-rest, but inference still transits to
  Anthropic (US) under SCCs. Confirm the Anthropic DPA + lawful basis + candidate
  privacy notice before loading real candidate data.
- **File storage at scale:** the Volume is fine for a single web instance. For
  multiple instances or strict EU residency of files, switch `raw_file` to object
  storage (S3 / Cloudflare R2 in an EU region) via `django-storages`.
- **Local parity:** `docker build -t staffinit . && docker run -p 8000:8000 -e DATABASE_URL=... -e ANTHROPIC_API_KEY=... staffinit`.
