"""Env-var credential backend.

Reads SNOWTUNER_SNOWFLAKE_* env vars.  Writes are not supported — env vars are
the user's shell / deployment's responsibility.
"""
from __future__ import annotations

import os

from snowtuner.credentials.model import AuthMethod, SnowflakeCredentials

PREFIX = "SNOWTUNER_SNOWFLAKE_"


def load() -> SnowflakeCredentials | None:
    account = os.environ.get(PREFIX + "ACCOUNT")
    user = os.environ.get(PREFIX + "USER")
    if not account or not user:
        return None
    auth_raw = os.environ.get(PREFIX + "AUTHENTICATOR") or "password"
    try:
        auth = AuthMethod(auth_raw.lower())
    except ValueError:
        # Unknown value — fall back to password and let the connector complain.
        auth = AuthMethod.PASSWORD
    return SnowflakeCredentials(
        account=account,
        user=user,
        auth_method=auth,
        password=os.environ.get(PREFIX + "PASSWORD"),
        private_key_path=os.environ.get(PREFIX + "PRIVATE_KEY_PATH"),
        warehouse=os.environ.get(PREFIX + "WAREHOUSE"),
        role=os.environ.get(PREFIX + "ROLE"),
    )
