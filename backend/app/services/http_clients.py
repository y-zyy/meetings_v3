"""Process-local, reusable HTTP clients for Celery workers."""

from __future__ import annotations

import atexit
from functools import lru_cache
from threading import Lock
from typing import Any

import httpx
from openai import OpenAI


_clients: list[Any] = []
_clients_lock = Lock()


def _track(client: Any):
    with _clients_lock:
        _clients.append(client)
    return client


@lru_cache(maxsize=8)
def get_http_client(timeout: int) -> httpx.Client:
    """Return a pooled HTTP client for the requested timeout."""
    return _track(httpx.Client(timeout=timeout))


@lru_cache(maxsize=16)
def _get_openai_client(base_url: str, api_key: str, timeout: int) -> OpenAI:
    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
    if base_url:
        kwargs["base_url"] = base_url
    return _track(OpenAI(**kwargs))


def get_openai_client(*, api_key: str, timeout: int, base_url: str | None = None) -> OpenAI:
    """Return a pooled OpenAI-compatible client for one effective configuration."""
    return _get_openai_client(base_url or "", api_key, timeout)


def close_http_clients() -> None:
    """Close pooled connections when a worker process exits."""
    with _clients_lock:
        clients = list(_clients)
        _clients.clear()

    get_http_client.cache_clear()
    _get_openai_client.cache_clear()
    for client in clients:
        close = getattr(client, "close", None)
        if close:
            try:
                close()
            except Exception:
                pass


atexit.register(close_http_clients)
