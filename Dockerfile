# Production image for both the web and worker services (same image, different
# start commands). Uses uv for reproducible installs and bakes collected static
# into the image so WhiteNoise can serve it at runtime.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Use hashed-manifest static storage at build (collectstatic) AND runtime
    # (WhiteNoise). Kept out of DEBUG so tests/local use plain storage.
    DJANGO_MANIFEST_STATIC=true

WORKDIR /app

# Install deps first (cached layer). --no-dev drops test-only deps (fpdf2).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# App code.
COPY . .
RUN uv sync --frozen --no-dev

# Bake static files into the image (no DB needed for collectstatic).
RUN DJANGO_SECRET_KEY=build-only DJANGO_DEBUG=false \
    uv run python manage.py collectstatic --noinput

EXPOSE 8000

# Web default. The worker service overrides this with:
#   uv run python manage.py procrastinate worker --concurrency 4
# Shell form so $PORT (set by Railway) is expanded.
CMD uv run gunicorn staffinit.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 3
