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

    # Patch 3: DatadogInitParams missing — comment out the broken import
    p_dd = base / "integrations/datadog/__init__.py"
    if p_dd.exists():
        txt_dd = p_dd.read_text()
        if "DatadogInitParams" in txt_dd and "cannot import" not in txt_dd:
            p_dd.write_text("\n".join(
                "# " + l if "DatadogInitParams" in l else l
                for l in txt_dd.splitlines()
            ) + "\nDatadogInitParams = None  # patched\n")
            print(f"patched datadog __init__: {p_dd}")

    # Patch 4: VertexAI ImportError — VertexAIModelRoute/VertexImageGeneration missing.
    # We don't use VertexAI; remove the broken import block + instantiation.
    p_main = base / "main.py"
    if p_main.exists():
        txt_main = p_main.read_text()
        marker = "from .llms.vertex_ai.image_generation.image_generation_handler import"
        if marker in txt_main:
            out, skip = [], False
            for line in txt_main.splitlines():
                if marker in line:
                    skip = True
                    out.append("# (vertex_ai image_generation removed by patch)")
                    continue
                if skip:
                    if line.strip() == ")":
                        skip = False
                    continue
                # Stub the instantiation — keep the name defined as None so
                # `from .main import *` still finds vertex_image_generation
                if "VertexImageGeneration()" in line or (
                    "vertex_image_generation" in line and "=" in line
                ):
                    out.append("vertex_image_generation = None  # patched")
                    continue
                out.append(line)
            p_main.write_text("\n".join(out))
            print(f"patched litellm main.py: removed vertex_ai image_generation")
    break
EOF

# Install Playwright + Chromium browser (used by crawl4ai and ScrapeGraphAI)
# --with-deps installs OS-level libraries (libglib, libnss, etc.) automatically
RUN playwright install chromium --with-deps

# Create data directories
RUN mkdir -p /app/data/mempalace

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
