"""Plaintext-TOML credential backend at ``~/.snowtuner/creds.toml`` (mode 0600).

Chosen over encryption-with-password-prompt because (a) encryption against a
local attacker with read access to the user's home dir is security theater
anyway, and (b) this matches the pragmatic convention of ``~/.aws/credentials``
and similar.  Users who need stronger protection should set env vars or rely
on the OS keychain backend.
"""
from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

from snowtuner.credentials.model import AuthMethod, SnowflakeCredentials
from snowtuner.storage.db import data_dir


def path() -> Path:
    return data_dir() / "creds.toml"


def load() -> SnowflakeCredentials | None:
    p = path()
    if not p.exists():
        return None
    try:
        with p.open("rb") as f:
            data = tomllib.load(f)
    except Exception:
        return None
    s = data.get("snowflake")
    if not s:
        return None
    try:
        return SnowflakeCredentials(
            account=s["account"],
            user=s["user"],
            auth_method=AuthMethod(s.get("auth_method", "key_pair")),
            password=s.get("password"),
            private_key_path=s.get("private_key_path"),
            warehouse=s.get("warehouse"),
            role=s.get("role"),
        )
    except Exception:
        return None


def store(creds: SnowflakeCredentials) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = _render(creds)
    # Write then chmod to 0600 — fresh files may inherit more-permissive umask.
    with p.open("w") as f:
        f.write(body)
    os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def delete() -> None:
    p = path()
    if p.exists():
        p.unlink()


def _render(creds: SnowflakeCredentials) -> str:
    lines = ["# snowtuner credentials — mode 0600, do not commit", "[snowflake]"]
    lines.append(f'account = "{_escape(creds.account)}"')
    lines.append(f'user = "{_escape(creds.user)}"')
    lines.append(f'auth_method = "{creds.auth_method.value}"')
    if creds.password is not None:
        lines.append(f'password = "{_escape(creds.password)}"')
    if creds.private_key_path:
        lines.append(f'private_key_path = "{_escape(creds.private_key_path)}"')
    if creds.warehouse:
        lines.append(f'warehouse = "{_escape(creds.warehouse)}"')
    if creds.role:
        lines.append(f'role = "{_escape(creds.role)}"')
    return "\n".join(lines) + "\n"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
