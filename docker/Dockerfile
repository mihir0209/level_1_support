FROM python:3.12-slim

WORKDIR /app

# System dependencies (libpq for psycopg, build tools for native extensions)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
RUN pip install uv

# Copy project files
COPY pyproject.toml .
COPY . .

# Install Python dependencies (production only)
RUN uv pip install --system -e .

# Patch litellm: some PyPI builds of 1.76.x ship an arize/__init__.py that
# references SupportedPromptIntegrations.ARIZE_PHOENIX before the enum member
# is defined. We remove the broken dict entry — we don't use Arize Phoenix.
RUN python3 - <<'EOF'
import pathlib, sys
for sp in sys.path:
    p = pathlib.Path(sp) / "litellm/integrations/arize/__init__.py"
    if p.exists():
        txt = p.read_text()
        if "ARIZE_PHOENIX" in txt:
            fixed = "\n".join(
                line for line in txt.splitlines()
                if "ARIZE_PHOENIX" not in line
            )
            p.write_text(fixed)
            print(f"patched {p}")
        break
EOF

# Install Playwright + Chromium browser (used by crawl4ai and ScrapeGraphAI)
# --with-deps installs OS-level libraries (libglib, libnss, etc.) automatically
RUN playwright install chromium --with-deps

# Create data directories
RUN mkdir -p /app/data/mempalace

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
