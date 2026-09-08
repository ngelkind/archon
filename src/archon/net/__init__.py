"""Network observability hooks that feed :class:`archon.netlog.NetLedger`.

Nothing here changes what a request does; each hook only records metadata about
it. Import these lazily from call sites so a missing optional dependency (e.g.
``google-auth-httplib2``) degrades to an unhooked-but-working client.
"""
