"""
Gunicorn production configuration for L1 Support API.

Workers: 2×CPU cores (min 2, max 8) — one uvicorn worker per CPU core.
Each worker: 1 asyncio event loop (--worker-class=uvicorn.workers.UvicornWorker).
Threads: 4 per worker for blocking DB/HTTP calls.

Timeouts:
  - worker TIMEOUT = 45s  (hard limit per request; LangGraph turns can take 10–30s)
  - graceful SHUTDOWN = 30s (let in-flight requests finish before killing worker)

Max requests per worker (memory leak guard): 1000
"""

from multiprocessing import cpu_count

bind = "0.0.0.0:8000"
worker_class = "uvicorn.workers.UvicornWorker"

# Scale: 2 workers per CPU core, capped at 8 total
workers = min(max(cpu_count() * 2, 2), 8)
threads = 4

# Timeouts
timeout = 45
timeout_keepalive = 10
graceful_timeout = 30

# Memory leak guard — recycle workers after N requests
max_requests = 1000
max_requests_jitter = 128

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(B)s "%(f)s" "%(a)s" %(D)sc)s'

# Pre-fork hook: warm up one DB connection before accepting traffic
def on_starting(server):
    server.log.info("gunicorn starting, PID=%s", server.pid)
