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

# Install Playwright + Chromium browser (used by crawl4ai and ScrapeGraphAI)
# --with-deps installs OS-level libraries (libglib, libnss, etc.) automatically
RUN playwright install chromium --with-deps

# Create data directories
RUN mkdir -p /app/data/mempalace

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
