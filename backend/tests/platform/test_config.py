"""Settings come from the environment, and the development secrets never reach a public deployment (T-01-04)."""

import pytest
from cryptography.fernet import Fernet

from app.platform import config

PUBLIC = "https://careers.example.org"
REAL_SECRETS = {
    "APP_DATA_KEYS": Fernet.generate_key().decode(),
    "APP_LOOKUP_KEY": "a-long-random-lookup-key-for-this-test",
    "APP_LINK_KEY": "a-long-random-link-key-for-this-test",
    "DATABASE_URL_MIGRATE": "postgresql://app:one-real-password@db/app",
    "DATABASE_URL_API": "postgresql://app_api:two-real-password@db/app",
    "DATABASE_URL_WORKER": "postgresql://app_worker:three-real-password@db/app",
}


def test_an_empty_environment_gives_the_development_settings():
    settings = config.load({})
    assert config.is_local(settings.app_base_url)
    assert settings.data_keys == (config.DEV_DATA_KEY,)
    assert settings.llm_provider == "anthropic" and settings.anthropic_api_key is None
    assert settings.worker_queues == ()


@pytest.mark.parametrize("url", ["http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"])
def test_development_secrets_are_allowed_on_localhost(url):
    config.load({"APP_BASE_URL": url})


def test_every_development_secret_is_refused_off_localhost():
    with pytest.raises(config.ConfigError) as refused:
        config.load({"APP_BASE_URL": PUBLIC})
    for name in (
        "APP_DATA_KEYS",
        "APP_LOOKUP_KEY",
        "APP_LINK_KEY",
        "DATABASE_URL_MIGRATE",
        "DATABASE_URL_API",
        "DATABASE_URL_WORKER",
    ):
        assert name in str(refused.value)


def test_real_secrets_are_accepted_off_localhost():
    settings = config.load({"APP_BASE_URL": PUBLIC, **REAL_SECRETS})
    assert settings.data_keys == (REAL_SECRETS["APP_DATA_KEYS"],)


def test_one_development_secret_is_enough_to_refuse():
    without_link_key = {k: v for k, v in REAL_SECRETS.items() if k != "APP_LINK_KEY"}
    with pytest.raises(config.ConfigError) as refused:
        config.load({"APP_BASE_URL": PUBLIC, **without_link_key})
    assert "APP_LINK_KEY" in str(refused.value) and "APP_LOOKUP_KEY" not in str(refused.value)


def test_a_data_key_that_is_not_a_fernet_key_is_refused_and_never_echoed():
    with pytest.raises(config.ConfigError) as refused:
        config.load({"APP_DATA_KEYS": "this-is-not-a-fernet-key"})
    assert "this-is-not-a-fernet-key" not in str(refused.value)


def test_several_data_keys_keep_their_order_and_empty_values_count_as_unset():
    first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    assert config.load({"APP_DATA_KEYS": f"{first}, {second}"}).data_keys == (first, second)
    assert config.load({"APP_LOOKUP_KEY": ""}).lookup_key == config.DEV_LOOKUP_KEY


def test_the_database_url_is_chosen_by_role():
    settings = config.load(REAL_SECRETS)
    assert "one-real-password" in settings.database_url("migrate")
    assert "two-real-password" in settings.database_url("api")
    assert "three-real-password" in settings.database_url("worker")
