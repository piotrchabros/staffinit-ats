#!/bin/sh
# Single entrypoint for both Railway services from one image.
# SERVICE_ROLE=worker  -> procrastinate worker (consumes scoring/generation jobs)
# (unset / anything else) -> gunicorn web server
set -e

if [ "$SERVICE_ROLE" = "worker" ]; then
    exec uv run python manage.py procrastinate worker --concurrency "${WORKER_CONCURRENCY:-4}"
fi

# --timeout 120: a bulk upload of hundreds parses many CVs in one request; the
# default 30s would kill the worker mid-upload.
exec uv run gunicorn staffinit.wsgi --bind "0.0.0.0:${PORT:-8000}" --workers 3 --timeout 120
