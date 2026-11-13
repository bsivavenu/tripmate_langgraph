FROM python:3.12-slim

# uv: fast installer that reads pyproject.toml + uv.lock (exact, reproducible versions)
COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /uvx /usr/local/bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Install dependencies first so this layer is cached until the lockfile changes
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# Copy the application code (see .dockerignore for what is excluded)
COPY . .

# Run as a non-root user
RUN useradd --create-home appuser
USER appuser

# The app launches the AviationStack MCP server with `uvx` (stdio subprocess).
# Pre-download it now so containers start fast and need no write access at runtime.
# aviationstack-mcp needs Python >= 3.13 (the app itself runs on 3.12), so uv downloads
# a managed 3.13 just for that tool; both caches live in appuser's home.
ENV UV_CACHE_DIR=/home/appuser/.cache/uv \
    UV_PYTHON_INSTALL_DIR=/home/appuser/.local/share/uv/python
RUN uvx --from aviationstack-mcp python -c "import aviationstack_mcp"

# Default port; hosts like Render override it with the PORT env var
ENV PORT=8000
EXPOSE 8000

# Bind to 0.0.0.0 so the app is reachable from outside the container
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
