"""seal_bytes / open_sealed_bytes: files are encrypted with the data keys and refuse to open if altered."""

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.platform import config, sessions


def test_bytes_round_trip_and_are_not_stored_in_clear():
    data = b"%PDF-1.7 invented resume \x00\xff"
    token = sessions.seal_bytes(data)
    assert data not in token
    assert sessions.open_sealed_bytes(token) == data


def test_an_altered_token_does_not_open():
    token = bytearray(sessions.seal_bytes(b"invented"))
    token[-5] ^= 1
    with pytest.raises(InvalidToken):
        sessions.open_sealed_bytes(bytes(token))


def test_a_file_sealed_with_an_old_key_still_opens_after_rotation(monkeypatch):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setattr(config, "get", lambda: config.load({"APP_ENV": "test", "APP_DATA_KEYS": old}))
    token = sessions.seal_bytes(b"invented")
    monkeypatch.setattr(config, "get", lambda: config.load({"APP_ENV": "test", "APP_DATA_KEYS": f"{new},{old}"}))
    assert sessions.open_sealed_bytes(token) == b"invented"
