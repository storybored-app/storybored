"""Settings API hardening: secret redaction, PUT key allow-listing, and the
LLM key-exfil guard in get_llm_config."""

import pytest
from sqlmodel import Session

from storybored.config import Settings
from storybored.db import create_db_engine, init_db
from storybored.llm.client import get_llm_config
from storybored.models import Setting


def test_get_settings_redacts_api_key(client):
    r = client.put("/api/settings", json={"values": {"llm_api_key": "sk-secret-123"}})
    assert r.status_code == 200, r.text

    body = client.get("/api/settings").json()
    # Raw key must not appear anywhere in the response.
    assert "sk-secret-123" not in str(body)
    assert "llm_api_key" not in body["overrides"]
    assert body["overrides"]["llm_api_key_set"] is True
    assert body["effective"]["llm_api_key_set"] is True
    assert "llm_api_key" not in body["effective"]


def test_put_settings_validates_style_loras(client):
    ok = '[{"lora_name": "styles/noir.safetensors", "strength": 0.6, "enabled": true}]'
    r = client.put("/api/settings", json={"values": {"style_loras": ok}})
    assert r.status_code == 200, r.text
    assert client.get("/api/settings").json()["overrides"]["style_loras"] == ok

    for bad in (
        "not json",
        '{"lora_name": "x"}',  # not a list
        '[{"strength": 1.0}]',  # missing lora_name
        '[{"lora_name": "x", "strength": "high"}]',  # non-numeric strength
        '[{"lora_name": "x", "strength": 99}]',  # out of range
        '[{"lora_name": "x", "enabled": "yes"}]',  # non-bool enabled
    ):
        r = client.put("/api/settings", json={"values": {"style_loras": bad}})
        assert r.status_code == 400, bad

    # good value still in place, and empty value clears it
    assert client.get("/api/settings").json()["overrides"]["style_loras"] == ok
    r = client.put("/api/settings", json={"values": {"style_loras": ""}})
    assert r.status_code == 200
    assert "style_loras" not in client.get("/api/settings").json()["overrides"]


def test_put_settings_rejects_unknown_key(client):
    r = client.put("/api/settings", json={"values": {"evil_key": "http://attacker"}})
    assert r.status_code == 400
    assert "evil_key" in r.json()["detail"]
    # nothing persisted
    assert "evil_key" not in client.get("/api/settings").json()["overrides"]


def test_put_settings_accepts_default_image_workflow(client):
    r = client.put(
        "/api/settings", json={"values": {"default_image_workflow": "krea2-basic"}}
    )
    assert r.status_code == 200, r.text
    assert (
        client.get("/api/settings").json()["overrides"]["default_image_workflow"]
        == "krea2-basic"
    )


@pytest.fixture
def db_session(tmp_path):
    settings = Settings(_env_file=None, data_dir=str(tmp_path / "data"))
    engine = create_db_engine(settings)
    init_db(engine)
    with Session(engine) as session:
        yield session, settings
    engine.dispose()


def test_env_key_not_forwarded_to_overridden_base_url(db_session):
    session, _ = db_session
    # env supplies both a base URL and a key...
    settings = Settings(
        _env_file=None,
        llm_base_url="http://env-host/v1",
        llm_api_key="env-secret",
        llm_model="m",
    )
    # ...but the user overrides ONLY the base URL to an attacker host.
    session.add(Setting(key="llm_base_url", value="http://attacker/v1"))
    session.commit()

    cfg = get_llm_config(session, settings)
    assert cfg.base_url == "http://attacker/v1"
    assert cfg.api_key == ""  # env key must NOT be sent to the override host


def test_overridden_key_is_forwarded_to_overridden_base_url(db_session):
    session, _ = db_session
    settings = Settings(
        _env_file=None, llm_base_url="http://env-host/v1", llm_api_key="env-secret"
    )
    session.add(Setting(key="llm_base_url", value="http://user-host/v1"))
    session.add(Setting(key="llm_api_key", value="user-key"))
    session.commit()

    cfg = get_llm_config(session, settings)
    assert cfg.base_url == "http://user-host/v1"
    assert cfg.api_key == "user-key"  # user supplied a key for their own host


def test_env_key_used_when_base_url_is_from_env(db_session):
    session, _ = db_session
    settings = Settings(
        _env_file=None, llm_base_url="http://env-host/v1", llm_api_key="env-secret"
    )
    cfg = get_llm_config(session, settings)
    assert cfg.base_url == "http://env-host/v1"
    assert cfg.api_key == "env-secret"  # env configured both -> fine
