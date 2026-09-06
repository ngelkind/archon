"""Test doubles that drive the REAL Archon code with fake transports.

Everything here is importable from the production package on purpose: the
end-to-end suite (``tests/e2e``), the developer console and the live probe
runner all reuse the same fakes, so a scenario proven offline can be replayed
against the live box with only the transport swapped.

Rules the doubles follow:

* They stand in at the TRANSPORT boundary (an HTTP server aiogram polls, a
  Telethon client object, a neonize client object, an LLM provider), never at
  the pipeline/tool/repo boundary - the point is that the real adapters, gate,
  triage, agent loop, tool dispatch and database run unchanged.
* They encode the real library's contract, including its warts (neonize's
  ``connect()`` returns a task; ``is_connected`` is an awaitable property),
  because a fake that is kinder than the real thing hides exactly the bugs
  that matter.
* They refuse rather than improvise: an LLM call no scenario scripted is an
  error the test sees, not a plausible answer that lets a broken path go green.
"""
