"""Per-tenant platform integrations.

Each module here knows how to link one provider to a tenant and how to build
that tenant's client. They register themselves with the session registry
(``rt.sessions.register_factory``) exactly as tools register with the tool
registry — so this package can grow a provider without the core knowing.
"""
