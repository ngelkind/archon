"""Product-only boot: the service must start with a fresh empty database and
NO owner secrets at all.

This is the deploy gate. The product runs as a separate service from the
owner's personal bot — different machine role, different data directory, no
`token.json`, no WhatsApp session, no Telethon session, no owner Telegram id.
Every owner-scoped subsystem has to stand down cleanly rather than crash-loop,
while the API and the product bot come up.

The tests deliberately assert on `rt.health`, because that is what an operator
reads to tell "correctly not running" from "broken".
"""

from __future__ import annotations

import asyncio
import os

import pytest

from archon.config import InsecureConfigError, Settings, check_production_secrets

_SECRETS = {
    "jwt_secret": "j" * 64,
    "api_token_pepper": "p" * 64,
    "credential_encryption_key": "c" * 64,
}


def _product_env(tmp_path, **extra) -> dict:
    """The minimum a product deployment sets — note: no owner credentials."""
    env = {
        "multitenant_enabled": True,
        "api_enabled": True,
        "api_bind_host": "127.0.0.1",
        "api_port": 8788,
        "archon_data": tmp_path / "data",
        "archon_secrets": tmp_path / "secrets",
        "gemini_api_key": "fake",
        "_env_file": None,
        **_SECRETS,
    }
    env.update(extra)
    return env


def _build(tmp_path, **extra):
    """build_runtime() with the environment isolated to tmp_path."""
    from archon import app

    settings = Settings(**_product_env(tmp_path, **extra))
    settings.ensure_dirs()
    check_production_secrets(settings)

    import archon.config as config_mod

    original = config_mod.load_settings
    config_mod.load_settings = lambda: settings
    app.load_settings = lambda: settings
    try:
        return app.build_runtime()
    finally:
        config_mod.load_settings = original
        app.load_settings = original


# --- boot --------------------------------------------------------------------

def test_build_runtime_starts_without_owner_credentials(tmp_path):
    """The old hard requirement on TELEGRAM_BOT_TOKEN/OWNER_ID would SystemExit
    here; a product deployment has neither."""
    rt = _build(tmp_path)
    assert rt.settings.multitenant_enabled is True
    assert not rt.settings.telegram_bot_token
    assert not rt.settings.telegram_owner_id
    assert rt.registry is not None and rt.router is not None


def test_personal_deployment_still_demands_owner_credentials(tmp_path):
    """The single-user guard must not have been loosened for everyone."""
    from archon import app

    settings = Settings(**_product_env(tmp_path, multitenant_enabled=False))
    settings.ensure_dirs()
    app.load_settings = lambda: settings
    try:
        with pytest.raises(SystemExit, match="MULTITENANT_ENABLED"):
            app.build_runtime()
    finally:
        import archon.config as config_mod

        app.load_settings = config_mod.load_settings


def test_fresh_database_migrates_and_seeds_the_owner_tenant(tmp_path):
    from archon.db import repo
    from archon.db.tenancy import OWNER_TENANT_ID, owner_scope

    rt = _build(tmp_path)
    assert rt.db.query_one("SELECT version FROM schema_version")["version"] >= 13
    # tenant 1 exists (migration 008) but owns nothing on a fresh product DB
    assert rt.db.query_one("SELECT 1 FROM users WHERE id = ?", (OWNER_TENANT_ID,))
    assert repo.chat_list(owner_scope(rt.db)) == []
    assert repo.contact_list(owner_scope(rt.db)) == []


def test_product_boot_refuses_placeholder_secrets(tmp_path):
    """Fail closed: a public listener must never run on the in-repo defaults."""
    for missing in ("jwt_secret", "api_token_pepper", "credential_encryption_key"):
        env = _product_env(tmp_path)
        env[missing] = ""
        with pytest.raises(InsecureConfigError, match=missing.upper()):
            check_production_secrets(Settings(**env))


# --- which subsystems start ---------------------------------------------------

def _run_main_briefly(rt, monkeypatch) -> dict:
    """Run main() long enough for every task to reach its first await, then
    cancel. Returns the health map an operator would see."""
    from archon import app

    monkeypatch.setattr(app, "build_runtime", lambda: rt)

    async def scenario():
        task = asyncio.create_task(app.main())
        await asyncio.sleep(0.1)
        snapshot = dict(rt.health)      # BEFORE cancelling, or all read "cancelled"
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        return snapshot

    return asyncio.run(scenario())


def test_owner_subsystems_stand_down_cleanly(tmp_path, monkeypatch):
    rt = _build(tmp_path)
    health = _run_main_briefly(rt, monkeypatch)

    # No owner credentials -> the control bot is deliberately not running, and
    # says so rather than crash-looping.
    assert "disabled" in health.get("control_bot", "")
    assert "no session" in health.get("whatsapp", "")
    # The userbot skips itself when unconfigured (clean return, not a crash).
    assert health.get("tg_userbot", "").startswith("not configured")
    for name, state in health.items():
        assert not state.startswith("crashed:"), f"{name} crashed: {state}"


def test_product_subsystems_come_up(tmp_path, monkeypatch):
    rt = _build(tmp_path, product_telegram_bot_token="123:fake",
                telegram_bot_username="ArchonProductBot")
    health = _run_main_briefly(rt, monkeypatch)

    assert health.get("pipeline") == "running"
    assert health.get("scheduler") == "running"
    # The Gmail poller must run in product mode even with no owner token file,
    # because it serves every tenant who linked Google.
    assert "gmail" in health and "no token" not in health["gmail"]
    # The product bot is supervised (it will fail to reach Telegram with a fake
    # token, which is the supervisor's job, not a boot failure).
    assert "product_bot" in health


def test_product_bot_is_off_without_its_own_token(tmp_path, monkeypatch):
    rt = _build(tmp_path)          # multitenant on, but no product bot token
    health = _run_main_briefly(rt, monkeypatch)
    assert "disabled" in health.get("product_bot", "")


# --- the API surface ----------------------------------------------------------

def test_api_mounts_auth_and_integrations_in_product_mode(tmp_path):
    from fastapi.routing import APIRoute

    from archon.api.server import build_app

    rt = _build(tmp_path)
    app_ = build_app(rt)
    paths = set()
    for inc in app_.routes:
        for route in getattr(inc, "original_router", inc).routes:
            if isinstance(route, APIRoute):
                paths.add(route.path)

    for required in ("/auth/signup", "/auth/login", "/auth/refresh", "/auth/me",
                     "/integrations/google/authorize",
                     "/integrations/telegram/link",
                     "/integrations/whatsapp/link"):
        assert required in paths, f"{required} not mounted"


def test_signup_then_me_works_with_no_owner_secrets(tmp_path):
    """End-to-end on a product-only boot: a stranger can create an account and
    read it back, with nothing of the owner's on disk."""
    from fastapi.testclient import TestClient

    from archon.api.server import build_app

    rt = _build(tmp_path)
    assert not rt.settings.google_token_path.exists()
    assert not rt.settings.wa_session_path.exists()

    client = TestClient(build_app(rt))
    signup = client.post("/auth/signup", json={"email": "new@example.com",
                                               "password": "correct horse battery"})
    assert signup.status_code in (200, 201), signup.text
    token = signup.json()["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "new@example.com"

    # the new account is a real tenant, distinct from the owner tenant
    from archon.db.tenancy import OWNER_TENANT_ID

    assert me.json()["id"] != OWNER_TENANT_ID


def test_rate_limiting_is_installed_on_the_public_surface(tmp_path):
    """The tunnel is public: the per-IP budget must actually be attached."""
    from archon.api.server import build_app

    rt = _build(tmp_path)
    app_ = build_app(rt)
    assert getattr(app_.state, "rate_limiter", None) is not None


def test_api_binds_loopback_not_all_interfaces(tmp_path):
    """cloudflared reaches the API over loopback; binding 0.0.0.0 on a public
    box would expose it directly."""
    rt = _build(tmp_path)
    assert rt.settings.api_bind_host == "127.0.0.1"
    assert not rt.settings.api_bind_host.startswith("0.0.0.0")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions only")
def test_secrets_directory_is_not_world_readable(tmp_path):
    rt = _build(tmp_path)
    from archon.integrations.whatsapp import session_dir

    mode = session_dir(rt, 2).stat().st_mode & 0o777
    assert mode == 0o700
