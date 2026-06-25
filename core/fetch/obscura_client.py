"""
Obscura CDP client wrapper.

Obscura (https://github.com/h4ckf0r0day/obscura) is a Rust-based headless browser
(~80MB binary vs 200MB+ for Chrome) designed for AI agents and web scraping.
It speaks Chrome DevTools Protocol (CDP) over WebSocket, making it a drop-in
playwright CDP endpoint replacement.

Usage:
    # Start Obscura CDP server
    obscura serve --port 9222 --stealth

    # Then in Python, connect via playwright:
    from playwright.async_api import async_playwright
    browser = await chromium.connect_over_cdp(
        endpoint_url="ws://127.0.0.1:9222"
    )

Performance comparison:
    | Metric       | Obscura  | Chrome  |
    |--------------|----------|---------|
    | Binary size   | ~80MB    | 300MB+  |
    | Memory usage  | ~30MB    | 200MB+  |
    | Page load     | ~85ms    | ~500ms  |
    | Startup       | instant  | ~2s     |

For the L1 support agent collector, Obscura can replace crawl4ai's stealth
playwright for faster page fetches. FireCrawl remains the fallback for
hard-to-scrape sites behind Cloudflare/Akamai.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────
OBSCURA_BINARY_PATH = Path(__file__).parent.parent.parent / "bin" / "obscura"
OBSCURA_DEFAULT_PORT = 9222
OBSCURA_STARTUP_TIMEOUT = 10  # seconds to wait for CDP server to be ready


@dataclass
class ObscuraConfig:
    """Configuration for Obscura CDP server."""
    port: int = OBSCURA_DEFAULT_PORT
    stealth: bool = True
    workers: int = 1
    verbose: bool = False

    @property
    def websocket_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/browser"


class ObscuraManager:
    """
    Manages the Obscura CDP server lifecycle.

    Can run as:
    1. Embedded process (managed by this class)
    2. External container/service (configured via environment)

    The agent collector will use this to get a playwright Browser instance
    connected to Obscura instead of launching a full Chrome browser.
    """

    def __init__(self, config: ObscuraConfig | None = None):
        self.config = config or ObscuraConfig()
        self._process: subprocess.Popen | None = None
        self._started = False

    @property
    def is_running(self) -> bool:
        """Check if the Obscura CDP server is running."""
        if self._started and self._process is not None:
            return self._process.poll() is None
        return False

    async def start(self) -> bool:
        """
        Start the Obscura CDP server.

        Returns True if successfully started, False if:
        - Binary not found or not executable
        - Server failed to start
        """
        if self.is_running:
            log.debug("obscura_already_running", port=self.config.port)
            return True

        binary = self._find_binary()
        if not binary:
            log.warning("obscura_binary_not_found",
                       hint=f"Download from https://github.com/h4ckf0r0day/obscura/releases")
            return False

        cmd = [
            str(binary),
            "serve",
            "--port", str(self.config.port),
            "--workers", str(self.config.workers),
        ]
        if self.config.stealth:
            cmd.append("--stealth")
        if self.config.verbose:
            cmd.append("--verbose")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            # Wait for server to be ready
            if await self._wait_for_server():
                self._started = True
                log.info("obscura_started", port=self.config.port, url=self.config.websocket_url)
                return True
            else:
                log.error("obscura_start_failed")
                self._process.kill()
                self._process = None
                return False
        except Exception as e:
            log.error("obscura_start_error", error=str(e))
            return False

    async def _wait_for_server(self) -> bool:
        """Wait for CDP server to respond to /json/version."""
        import httpx
        url = f"http://127.0.0.1:{self.config.port}/json/version"
        for _ in range(OBSCURA_STARTUP_TIMEOUT * 2):
            try:
                async with httpx.AsyncClient(timeout=2) as client:
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        return False

    def stop(self) -> None:
        """Stop the Obscura CDP server."""
        if self._process:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
            self._started = False
            log.info("obscura_stopped")

    def _find_binary(self) -> Path | None:
        """Find the Obscura binary."""
        # Check project bin directory
        if OBSCURA_BINARY_PATH.exists():
            return OBSCURA_BINARY_PATH

        # Check PATH
        import shutil
        path = shutil.which("obscura")
        if path:
            return Path(path)

        return None


async def get_playwright_browser_via_obscura(
    config: ObscuraConfig | None = None,
) -> Any | None:
    """
    Get a playwright Browser instance connected to Obscura CDP server.

    Returns None if Obscura is not available. Falls back to None so callers
    can use their own playwright browser launch strategy.

    Usage:
        browser = await get_playwright_browser_via_obscura()
        if browser:
            page = await browser.new_page()
            await page.goto("https://example.com")
    """
    cfg = config or ObscuraConfig()

    try:
        from playwright.async_api import async_playwright, Error as PlaywrightError

        # Try to connect to Obscura CDP server
        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cfg.websocket_url)
                log.info("obscura_connected", url=cfg.websocket_url)
                return browser
            except PlaywrightError as e:
                log.debug("obscura_connect_failed", error=str(e))
                return None
    except ImportError:
        log.debug("playwright_not_available")
        return None


# ── Standalone fetch (no playwright dependency) ───────────────────────────

async def obscura_fetch(
    url: str,
    dump: str = "html",
    stealth: bool = True,
    wait: int = 5,
    wait_until: str = "load",
    user_agent: str | None = None,
) -> dict[str, Any]:
    """
    Fetch a URL using the Obscura CLI directly (no playwright needed).

    This is useful for quick fetches where you don't need the full CDP session.

    Args:
        url: URL to fetch
        dump: Output format - "html", "text", or "links"
        stealth: Enable anti-detection measures
        wait: Seconds to wait after page load
        wait_until: Wait condition - "load", "domcontentloaded", "networkidle0"
        user_agent: Custom User-Agent string

    Returns:
        {"success": bool, "html": str, "text": str, "links": list, "error": str}
    """
    binary = ObscuraManager()._find_binary()
    if not binary:
        return {"success": False, "error": "Obscura binary not found"}

    cmd = [
        str(binary), "fetch",
        "--dump", dump,
        "--wait", str(wait),
        "--wait-until", wait_until,
    ]
    if stealth:
        cmd.append("--stealth")
    if user_agent:
        cmd.extend(["--user-agent", user_agent])
    cmd.append(url)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)

        if proc.returncode != 0:
            return {"success": False, "error": stderr.decode() if stderr else "Unknown error"}

        output = stdout.decode().strip()
        result: dict[str, Any] = {"success": True}

        if dump == "html":
            result["html"] = output
        elif dump == "text":
            result["text"] = output
        elif dump == "links":
            try:
                result["links"] = json.loads(output)
            except json.JSONDecodeError:
                result["links"] = [line.strip() for line in output.split("\n") if line.strip()]

        return result

    except asyncio.TimeoutError:
        return {"success": False, "error": "Fetch timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Scrape multiple URLs ─────────────────────────────────────────────────

async def obscura_scrape(
    urls: list[str],
    concurrency: int = 5,
    stealth: bool = True,
) -> list[dict[str, Any]]:
    """
    Scrape multiple URLs in parallel using Obscura.

    NOTE: Requires building from source (`cargo build --release`) to get the
    worker binary. Without it, scrape returns an error.

    Args:
        urls: List of URLs to scrape
        concurrency: Number of parallel workers
        stealth: Enable anti-detection measures (note: not supported in pre-built binary)

    Returns:
        List of result dicts with url, success, html, text, links, error
    """
    binary = ObscuraManager()._find_binary()
    if not binary:
        return [{"url": url, "success": False, "error": "Obscura binary not found"} for url in urls]

    cmd = [
        str(binary), "scrape",
        "--concurrency", str(concurrency),
        "--timeout", "30",
    ]
    cmd.extend(urls)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            err_msg = stderr.decode() if stderr else "Unknown error"
            return [{"url": url, "success": False, "error": err_msg} for url in urls]

        output = stdout.decode().strip()
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            return [{"url": url, "success": True, "html": output} for url in urls]

    except asyncio.TimeoutError:
        return [{"url": u, "success": False, "error": "Scrape timed out"} for u in urls]
    except Exception as e:
        return [{"url": u, "success": False, "error": str(e)} for u in urls]


# ── Health check ─────────────────────────────────────────────────────────

async def check_obscura_health(port: int = OBSCURA_DEFAULT_PORT) -> dict[str, Any]:
    """
    Check if Obscura CDP server is running and healthy.

    Returns:
        {"available": bool, "version": str, "protocol_version": str, "error": str}
    """
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/json/version")
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "available": True,
                    "version": data.get("Browser", "unknown"),
                    "protocol_version": data.get("Protocol-Version", "unknown"),
                    "websocket_url": data.get("webSocketDebuggerUrl", ""),
                }
            return {"available": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"available": False, "error": str(e)}
