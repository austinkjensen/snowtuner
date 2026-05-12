"""Credential model + backend enum."""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AuthMethod(str, Enum):
    KEY_PAIR = "key_pair"
    PASSWORD = "password"
    EXTERNAL_BROWSER = "externalbrowser"


class CredentialBackend(str, Enum):
    ENV = "env"
    KEYRING = "keyring"
    FILE = "file"


class SnowflakeCredentials(BaseModel):
    """In-memory Snowflake credentials.  Never persisted to DuckDB.

    The recommended configuration is ``auth_method = KEY_PAIR`` with a dedicated
    ``TYPE=SERVICE`` Snowflake user (see ``snowtuner bootstrap-sql``).
    ``password`` and ``externalbrowser`` modes are kept for dev/test convenience.
    """

    account: str = Field(..., description="e.g. 'xy12345.us-east-1'")
    user: str
    auth_method: AuthMethod = AuthMethod.KEY_PAIR
    password: str | None = None
    private_key_path: str | None = Field(
        None,
        description="Filesystem path to the PEM-encoded PKCS8 private key "
                    "for KEY_PAIR auth.",
    )
    warehouse: str | None = None
    role: str | None = None

    def to_connector_kwargs(self) -> dict[str, Any]:
        """Render the kwargs accepted by ``snowflake.connector.connect(...)``."""
        kwargs: dict[str, Any] = {"account": self.account, "user": self.user}
        if self.auth_method == AuthMethod.KEY_PAIR:
            if not self.private_key_path:
                raise ValueError("key_pair auth requires private_key_path")
            key_path = Path(self.private_key_path).expanduser()
            if not key_path.exists():
                raise FileNotFoundError(
                    f"Private key file not found: {key_path}.  "
                    f"Run `snowtuner init` to regenerate."
                )
            kwargs["private_key_file"] = str(key_path)
        elif self.auth_method == AuthMethod.PASSWORD:
            if not self.password:
                raise ValueError("password auth requires a password")
            kwargs["password"] = self.password
        elif self.auth_method == AuthMethod.EXTERNAL_BROWSER:
            kwargs["authenticator"] = "externalbrowser"
        if self.warehouse:
            kwargs["warehouse"] = self.warehouse
        if self.role:
            kwargs["role"] = self.role
        return kwargs

    def redacted(self) -> dict[str, Any]:
        """Dict form safe to log — strips password."""
        d = self.model_dump(mode="json")
        if d.get("password"):
            d["password"] = "***"
        return d
