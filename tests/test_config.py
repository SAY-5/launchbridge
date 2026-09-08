import pytest

from launchbridge.config import Settings, _parse_mapping
from launchbridge.destinations import DestinationRegistry, expand_env


def test_parse_mapping_accepts_pairs_and_json():
    assert _parse_mapping("a=1, b=2") == {"a": "1", "b": "2"}
    assert _parse_mapping('{"a": "1"}') == {"a": "1"}
    assert _parse_mapping("") == {}
    with pytest.raises(ValueError):
        _parse_mapping("novalue")


def test_settings_normalise_driver_and_mappings():
    settings = Settings(
        database_url="postgres://u:p@h/db",
        webhook_secrets="shop=s1,crm=s2",
        admin_api_keys='{"ops": "k"}',
    )
    assert settings.database_url == "postgresql+psycopg://u:p@h/db"
    assert settings.webhook_secrets == {"shop": "s1", "crm": "s2"}
    assert settings.admin_api_keys == {"ops": "k"}


def test_expand_env_uses_values_defaults_and_errors():
    env = {"HOST": "receiver"}
    assert expand_env("http://${HOST}:${PORT:-8081}", env) == "http://receiver:8081"
    with pytest.raises(KeyError):
        expand_env("${MISSING}", env)


def test_registry_routing_and_validation():
    registry = DestinationRegistry.from_yaml(
        """
destinations:
  - {name: crm, url: "http://r/crm", secret: s, sources: ["*"]}
  - {name: billing, url: "http://r/billing", secret: s, sources: ["orders"]}
""",
    )
    assert [d.name for d in registry.for_source("orders")] == ["crm", "billing"]
    assert [d.name for d in registry.for_source("other")] == ["crm"]
    assert registry.get("billing").policy.max_attempts == 5
    assert registry.get("nope") is None


def test_registry_rejects_duplicates_and_bad_urls():
    with pytest.raises(ValueError):
        DestinationRegistry.from_yaml(
            'destinations:\n  - {name: a, url: "http://x", secret: s}\n'
            '  - {name: a, url: "http://y", secret: s}\n'
        )
    with pytest.raises(ValueError):
        DestinationRegistry.from_yaml('destinations:\n  - {name: a, url: "ftp://x", secret: s}\n')


def test_settings_parse_pair_strings_from_environment(monkeypatch):
    monkeypatch.setenv("LAUNCHBRIDGE_WEBHOOK_SECRETS", "smoke=s1,demo=s2")
    monkeypatch.setenv("LAUNCHBRIDGE_ADMIN_API_KEYS", '{"dev": "k"}')
    settings = Settings()
    assert settings.webhook_secrets == {"smoke": "s1", "demo": "s2"}
    assert settings.admin_api_keys == {"dev": "k"}
