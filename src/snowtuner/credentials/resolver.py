"""Tiered credential resolver: env → keyring → file.

Reads are tried in order — first hit wins.  Writes go to the best-available
backend (keyring if it exists, else file).  Callers can force a particular
backend for write via the ``backend`` arg to :meth:`store`.
"""
from __future__ import annotations

from dataclasses import dataclass

from snowtuner.credentials import env_backend, file_backend, keyring_backend
from snowtuner.credentials.model import CredentialBackend, SnowflakeCredentials


@dataclass
class ResolveResult:
    credentials: SnowflakeCredentials
    source: CredentialBackend


class CredentialResolver:
    def load(self) -> ResolveResult | None:
        creds = env_backend.load()
        if creds is not None:
            return ResolveResult(creds, CredentialBackend.ENV)
        creds = keyring_backend.load()
        if creds is not None:
            return ResolveResult(creds, CredentialBackend.KEYRING)
        creds = file_backend.load()
        if creds is not None:
            return ResolveResult(creds, CredentialBackend.FILE)
        return None

    def store(
        self,
        creds: SnowflakeCredentials,
        backend: CredentialBackend | None = None,
    ) -> CredentialBackend:
        """Write credentials.  If backend is None, picks keyring when available else file.
        Returns the backend that was actually used."""
        if backend is None:
            backend = (
                CredentialBackend.KEYRING
                if keyring_backend.available()
                else CredentialBackend.FILE
            )
        if backend == CredentialBackend.KEYRING:
            keyring_backend.store(creds)
        elif backend == CredentialBackend.FILE:
            file_backend.store(creds)
        else:
            raise ValueError(
                f"cannot write to backend {backend.value!r} "
                "(env vars are not writable by snowtuner)"
            )
        return backend

    def delete(self) -> None:
        """Best-effort delete from keyring and file backends.  Env vars are left alone."""
        keyring_backend.delete()
        file_backend.delete()

    def available_backends(self) -> list[CredentialBackend]:
        """Which backends can we write to right now?"""
        out = []
        if keyring_backend.available():
            out.append(CredentialBackend.KEYRING)
        out.append(CredentialBackend.FILE)  # always available
        return out
