"""OS-keychain credential backend (via python-keyring).

Uses the platform's native credential store: macOS Keychain, Windows Credential
Manager, Linux Secret Service (where available).  On headless Linux the library
falls back to a null backend and raises — we catch that and return None so the
file backend can take over.
"""
from __future__ import annotations

import json
import logging

from snowtuner.credentials.model import AuthMethod, SnowflakeCredentials

SERVICE = "snowtuner"
KEY = "snowflake"

log = logging.getLogger(__name__)


def available() -> bool:
    """True if the keyring library is importable and has a working backend."""
    try:
        import keyring
        import keyring.errors
    except ImportError:
        return False
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    # The null backend's class name contains 'fail' — treat as unavailable.
    return "fail" not in backend.__class__.__name__.lower()


def load() -> SnowflakeCredentials | None:
    if not available():
        return None
    import keyring
    try:
        raw = keyring.get_password(SERVICE, KEY)
    except Exception as e:
        log.warning("keyring load failed: %r", e)
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        data["auth_method"] = AuthMethod(data.get("auth_method", "key_pair"))
        return SnowflakeCredentials(**data)
    except Exception as e:
        log.warning("keyring contained malformed credentials: %r", e)
        return None


def store(creds: SnowflakeCredentials) -> None:
    if not available():
        raise RuntimeError("keyring backend is not available on this system")
    import keyring
    payload = json.dumps(creds.model_dump(mode="json"))
    keyring.set_password(SERVICE, KEY, payload)


def delete() -> None:
    if not available():
        return
    import keyring
    try:
        keyring.delete_password(SERVICE, KEY)
    except Exception:
        pass
