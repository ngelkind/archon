"""Shared pytest configuration.

Markers:
  e2e  — drives the real runtime with fake transports (offline, no keys).
  live — needs the VM's real sessions; never selected by default.

Cross-file imports inside tests (``from test_m2 import make_rt``) work because
pytest inserts this directory into ``sys.path`` for the conftest; ``tests/e2e``
relies on that too.
"""

from __future__ import annotations

import asyncio
import functools

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "e2e: end-to-end scenario over fake transports")
    config.addinivalue_line("markers", "live: needs real platform sessions on the VM")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("-m"):
        return
    skip_live = pytest.mark.skip(reason="live probes run on the VM only (-m live)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


def run_async(fn):
    """Run an ``async def test_…`` on a fresh event loop (no pytest-asyncio)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper
