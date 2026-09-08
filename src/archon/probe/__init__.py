"""Live probe runner (L3 testing).

Where the e2e harness (L1) drives FAKE transports offline, a probe drives the
REAL platforms from a SEPARATE test account and asserts on an OBSERVED effect —
an audit event, a DB row, a calendar entry, a post read back from the log
channel — never on "the call returned". It runs on the VM, gated behind
``probe.enabled`` and a distinct-session check, and writes ``data/probe.result.json``.
"""

from .runner import Probe, ProbeCtx, ProbeResult, run_probes  # noqa: F401
