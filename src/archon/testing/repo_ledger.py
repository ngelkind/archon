"""Record the store handed to every ``repo.*`` call.

Lifted from ``tests/test_tool_dispatch_scoping.py`` so end-to-end scenarios and
the live probe runner share it. Recording rather than raising is deliberate:
``Registry.dispatch`` swallows every exception into a JSON error string, so an
exception raised inside a tool would be lost, whereas the ledger survives and
can be asserted on afterwards.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from typing import Iterator

from ..db import repo
from ..db.tenancy import Db, TenantScope

_WRITE_VERBS = ("upsert", "add", "set", "create", "mark", "record", "delete",
                "expire", "claim", "purge", "cache", "import", "remember", "prune")


class Ledger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def record(self, fn_name: str, store: object) -> None:
        self.calls.append((fn_name, store))

    @property
    def offenders(self) -> list[str]:
        """Repo calls whose store is neither a Db nor a TenantScope — a Runtime,
        a ToolContext, or anything else reaching the data layer where a scope
        belongs. That is the whole bug class."""
        return [
            f"repo.{name}(<{type(store).__name__}>)"
            for name, store in self.calls
            if not isinstance(store, (Db, TenantScope))
        ]

    def tenants(self) -> set[int]:
        return {s.tenant_id for _, s in self.calls if isinstance(s, TenantScope)}

    def reached_repo(self) -> bool:
        return bool(self.calls)

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def writes(self) -> list[str]:
        """Calls whose name marks a mutation."""
        return [n for n in self.names() if any(v in n for v in _WRITE_VERBS)]


def wrap_repo(ledger: Ledger) -> dict[str, object]:
    """Replace every public repo function with a recording wrapper and return
    the originals for :func:`unwrap_repo`. Wrapping the module wholesale means
    a repo function added tomorrow is covered automatically."""
    originals: dict[str, object] = {}
    for name, fn in list(vars(repo).items()):
        if name.startswith("_") or not inspect.isfunction(fn):
            continue
        if not fn.__module__.endswith("repo"):
            continue  # re-exported helper, not a repo function

        def make(fn_name=name, real=fn):
            def wrapper(store=None, *args, **kwargs):
                ledger.record(fn_name, store)
                return real(store, *args, **kwargs)
            wrapper.__wrapped__ = real  # type: ignore[attr-defined]
            wrapper.__name__ = fn_name
            wrapper.__module__ = real.__module__
            return wrapper

        originals[name] = fn
        setattr(repo, name, make())
    return originals


def unwrap_repo(originals: dict[str, object]) -> None:
    for name, fn in originals.items():
        setattr(repo, name, fn)


@contextmanager
def recording_repo(ledger: Ledger | None = None) -> Iterator[Ledger]:
    led = ledger or Ledger()
    originals = wrap_repo(led)
    try:
        yield led
    finally:
        unwrap_repo(originals)
