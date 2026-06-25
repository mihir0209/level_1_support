import os
from core.tools.backends.interface import ToolBackend
from core.tools.backends.mock_backend import MockBackend
from core.tools.backends.stub_backend import StubBackend
from core.tools.backends.live_backend import LiveBackend

_ACTIVE_BACKEND: ToolBackend | None = None

def get_tool_backend() -> ToolBackend:
    """
    Resolve and return the active ToolBackend singleton instance based on the
    TOOL_BACKEND environment variable ('mock', 'stub', or 'live').
    """
    global _ACTIVE_BACKEND
    if _ACTIVE_BACKEND is not None:
        return _ACTIVE_BACKEND

    mode = os.environ.get("TOOL_BACKEND", "mock").lower().strip()
    if mode == "live":
        _ACTIVE_BACKEND = LiveBackend()
    elif mode == "stub":
        _ACTIVE_BACKEND = StubBackend()
    else:
        _ACTIVE_BACKEND = MockBackend()

    return _ACTIVE_BACKEND

def set_tool_backend(backend: ToolBackend | None) -> None:
    """Explicitly override the tool backend instance (mainly used for unit testing)."""
    global _ACTIVE_BACKEND
    _ACTIVE_BACKEND = backend
