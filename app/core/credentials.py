"""The application's database credential, protected by Windows DPAPI.

# What is stored, and what is not

The PostgreSQL administrator password is *never* stored. The installer uses it
once, to create a dedicated least-privilege role with its own randomly
generated password, and then discards it. Only that generated credential
reaches this module.

# Why DPAPI rather than a file with tight permissions

An ACL keeps other *users* out but not an administrator, and it does nothing at
all once the file is copied off the machine — a backup, a synced folder, a
stolen disk. ``CryptProtectData`` encrypts against a key derived from the
Windows account, so the ciphertext is worthless anywhere else and to anyone
else, with no key for this application to manage or leak.

The scope is deliberately the *user*, not the machine. ``CRYPTPROTECT_LOCAL_
MACHINE`` would let any account on the computer — including a service account
an attacker reached — recover the password. Per-user means the credential is
recoverable only by the person it belongs to, which is also why the installer
insists on being run by the account that will use the application.

# Consequences this module cannot hide

A credential written by one Windows account cannot be read by another. That is
the security property working, not a defect, so ``load_credential`` reports it
as its own outcome rather than as a generic failure — the caller can then tell
the user to reinstall under their own account instead of showing "decryption
failed".
"""

from __future__ import annotations

import ctypes
import json
import os
import secrets
import string
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from app.core import paths

#: Written as the first field so a file from a future version can be rejected
#: with a clear message rather than mis-parsed.
CREDENTIAL_FORMAT_VERSION = 1

#: Descriptive label DPAPI stores alongside the ciphertext. Visible in some
#: audit tooling; it holds no secret.
_DPAPI_DESCRIPTION = "Rising Path Recruiters Job Finding Automation database credential"


class CredentialError(RuntimeError):
    """A credential could not be stored or retrieved."""


class CredentialNotFound(CredentialError):
    """No credential file exists. The application has not been provisioned."""


class CredentialNotOurs(CredentialError):
    """The credential exists but belongs to a different Windows account."""


@dataclass(frozen=True, slots=True)
class DatabaseCredential:
    """Everything needed to connect as the application role."""

    username: str
    password: str
    host: str
    port: int
    database: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": CREDENTIAL_FORMAT_VERSION,
            "username": self.username,
            "password": self.password,
            "host": self.host,
            "port": self.port,
            "database": self.database,
        }

    @property
    def sqlalchemy_url(self) -> str:
        """Async URL. Quoted, because a generated password may contain
        characters that are otherwise significant in a URL."""
        from urllib.parse import quote

        return (
            f"postgresql+asyncpg://{quote(self.username, safe='')}:"
            f"{quote(self.password, safe='')}@{self.host}:{self.port}/{self.database}"
        )


# --- Password generation ----------------------------------------------------

#: Letters and digits only. Deliberately no punctuation: this password travels
#: through a PowerShell command line, an Inno Setup exec call, a libpq
#: connection string and a SQLAlchemy URL, and every one of those has its own
#: quoting rules. A 48-character alphanumeric secret carries ~285 bits of
#: entropy, far past anything quoting complexity would buy.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_PASSWORD_LENGTH = 48


def generate_password(length: int = _PASSWORD_LENGTH) -> str:
    """A cryptographically random password for the application role."""
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


# --- DPAPI ------------------------------------------------------------------


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(blob: _DataBlob) -> None:
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def is_supported() -> bool:
    """Whether DPAPI is available. False on any non-Windows platform."""
    return sys.platform == "win32"


def protect(plaintext: bytes) -> bytes:
    """Encrypt for the current Windows user."""
    if not is_supported():
        raise CredentialError("DPAPI is only available on Windows.")

    in_blob = _blob(plaintext)
    out_blob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        _DPAPI_DESCRIPTION,
        None,  # no additional entropy: the user's key is the whole control
        None,
        None,
        0,  # per-user scope; NOT CRYPTPROTECT_LOCAL_MACHINE
        ctypes.byref(out_blob),
    )
    if not ok:
        raise CredentialError(
            "Windows could not protect the credential on this computer "
            f"(error {ctypes.GetLastError()})."
        )
    try:
        return _blob_bytes(out_blob)
    finally:
        _free(out_blob)


def unprotect(ciphertext: bytes) -> bytes:
    """Decrypt data produced by :func:`protect` for this same Windows user."""
    if not is_supported():
        raise CredentialError("DPAPI is only available on Windows.")

    in_blob = _blob(ciphertext)
    out_blob = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        # The usual cause is a different Windows account, which is a distinct
        # situation with a distinct remedy, so it gets its own exception.
        raise CredentialNotOurs(
            "The stored database credential could not be read by this Windows "
            "account. It was created by a different account — reinstall while "
            "signed in as the account that will use the application."
        )
    try:
        return _blob_bytes(out_blob)
    finally:
        _free(out_blob)


# --- Storage ----------------------------------------------------------------


def save_credential(credential: DatabaseCredential, path: Path | None = None) -> Path:
    """Encrypt and write the credential, replacing any existing one.

    Written to a temporary file and moved into place so an interrupted write
    cannot leave a half-written credential that fails to decrypt.
    """
    target = path or paths.credential_file()
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(credential.as_dict(), separators=(",", ":")).encode("utf-8")
    ciphertext = protect(payload)

    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(ciphertext)
    os.replace(temporary, target)
    return target


def load_credential(path: Path | None = None) -> DatabaseCredential:
    """Read and decrypt the credential.

    Raises :class:`CredentialNotFound` when the application has not been
    provisioned, and :class:`CredentialNotOurs` when it belongs to another
    Windows account.
    """
    source = path or paths.credential_file()
    if not source.exists():
        raise CredentialNotFound(
            f"No database credential at {source}. The application has not been "
            "set up on this computer."
        )

    payload = json.loads(unprotect(source.read_bytes()).decode("utf-8"))

    version = payload.get("version")
    if version != CREDENTIAL_FORMAT_VERSION:
        raise CredentialError(
            f"The stored credential is version {version}, but this build "
            f"understands version {CREDENTIAL_FORMAT_VERSION}. Reinstall the "
            "application."
        )

    try:
        return DatabaseCredential(
            username=payload["username"],
            password=payload["password"],
            host=payload["host"],
            port=int(payload["port"]),
            database=payload["database"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise CredentialError(
            "The stored database credential is incomplete or corrupted. "
            "Reinstall the application to recreate it."
        ) from error


def credential_exists(path: Path | None = None) -> bool:
    """Whether a credential file is present. Does not attempt to decrypt it."""
    return (path or paths.credential_file()).exists()


__all__ = [
    "CREDENTIAL_FORMAT_VERSION",
    "CredentialError",
    "CredentialNotFound",
    "CredentialNotOurs",
    "DatabaseCredential",
    "credential_exists",
    "generate_password",
    "is_supported",
    "load_credential",
    "protect",
    "save_credential",
    "unprotect",
]
