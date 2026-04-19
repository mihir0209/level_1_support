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

# Patch litellm post-install bugs (defensive — applies only if the broken
# code is present, so safe across litellm versions):
#  1. arize/__init__.py may reference ARIZE_PHOENIX before it's in the enum
#  2. litellm_logging.py does `"key=" in api_base` when api_base is None
RUN python3 - <<'EOF'
import pathlib, sys

for sp in sys.path:
    base = pathlib.Path(sp) / "litellm"
    if not base.exists():
        continue

    # Patch 1: arize ARIZE_PHOENIX AttributeError
    p = base / "integrations/arize/__init__.py"
    if p.exists():
        txt = p.read_text()
        if "ARIZE_PHOENIX" in txt:
            fixed = "\n".join(l for l in txt.splitlines() if "ARIZE_PHOENIX" not in l)
            p.write_text(fixed)
            print(f"patched arize __init__: {p}")

    # Patch 2: logging TypeError when api_base is None
    p2 = base / "litellm_core_utils/litellm_logging.py"
    if p2.exists():
        txt2 = p2.read_text()
        if 'if "key=" in api_base:' in txt2:
            p2.write_text(txt2.replace(
                'if "key=" in api_base:',
                'if api_base and "key=" in api_base:',
            ))
            print(f"patched litellm_logging: {p2}")

    # Patch 3: VertexAI image generation ImportError (VertexAIModelRoute missing)
    p3 = base / "llms/vertex_ai/image_generation/__init__.py"
    if p3.exists():
        txt3 = p3.read_text()
        if "VertexAIModelRoute" in txt3 and "from litellm.llms.vertex_ai.common_utils" in txt3:
            # Wrap the problematic import in a try/except
            p3.write_text(txt3.replace(
                "from litellm.llms.vertex_ai.common_utils import (",
                "try:\n    from litellm.llms.vertex_ai.common_utils import (",
            ).replace(
                "    VertexAIModelRoute,\n)",
                "    VertexAIModelRoute,\n)\nexcept ImportError:\n    VertexAIModelRoute = None",
            ))
            print(f"patched vertex_ai image_generation __init__: {p3}")
    break
EOF

# Install Playwright + Chromium browser (used by crawl4ai and ScrapeGraphAI)
# --with-deps installs OS-level libraries (libglib, libnss, etc.) automatically
RUN playwright install chromium --with-deps

# Create data directories
RUN mkdir -p /app/data/mempalace

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
