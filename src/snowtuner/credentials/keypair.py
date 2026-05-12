"""RSA key-pair generation for Snowflake service-user auth.

Produces an unencrypted PKCS8 PEM private key at 0600 and returns the
corresponding public key in the format Snowflake's ``RSA_PUBLIC_KEY`` expects
(base64 blob, no header/footer lines).
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from snowtuner.storage.db import data_dir

DEFAULT_KEY_FILENAME = "snowtuner_rsa_key.p8"
DEFAULT_KEY_SIZE = 2048


@dataclass
class GeneratedKeyPair:
    private_key_path: Path
    public_key_pem: str       # full PEM with ``-----BEGIN PUBLIC KEY-----`` etc.
    public_key_snowflake: str  # base64 blob Snowflake's RSA_PUBLIC_KEY expects


def generate(
    *, key_path: Path | None = None, key_size: int = DEFAULT_KEY_SIZE
) -> GeneratedKeyPair:
    """Generate a fresh RSA keypair.  Writes private key to ``key_path`` at 0600.

    Overwrites any existing file at that path.
    """
    key_path = key_path or (data_dir() / DEFAULT_KEY_FILENAME)
    key_path.parent.mkdir(parents=True, exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    with key_path.open("wb") as f:
        f.write(private_pem)
    os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)

    public_pem = public_pem_bytes.decode("ascii")
    public_blob = _strip_pem_headers(public_pem)
    return GeneratedKeyPair(
        private_key_path=key_path,
        public_key_pem=public_pem,
        public_key_snowflake=public_blob,
    )


def public_blob_from_private(private_key_path: Path) -> str:
    """Re-derive the Snowflake-style public key blob from a stored private key.

    Used when the admin needs to paste the GRANT/ALTER RSA_PUBLIC_KEY a second
    time and we don't have the PEM cached.
    """
    with private_key_path.open("rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return _strip_pem_headers(public_pem)


def _strip_pem_headers(pem: str) -> str:
    """Strip PEM header/footer/newlines so Snowflake's ``RSA_PUBLIC_KEY`` accepts it."""
    lines = [
        line for line in pem.splitlines()
        if line and not line.startswith("-----")
    ]
    return "".join(lines)
