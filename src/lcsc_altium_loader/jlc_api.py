"""JLC Open Platform request signing and credential readiness checks.

The Components & MRO application may still be under review, so this module
implements only the documented, reusable authentication boundary.  It does
not guess business endpoint paths or persist secrets.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import string
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

JLC_OPEN_API_BASE_URL = "https://open-api.jlc.com"
APP_ID_ENV = "JLC_OPEN_APP_ID"
ACCESS_KEY_ENV = "JLC_OPEN_ACCESS_KEY"
SECRET_KEY_ENV = "JLC_OPEN_SECRET_KEY"
_NONCE_RE = re.compile(r"^[A-Za-z0-9]{32}$")
_HEADER_VALUE_RE = re.compile(r'^[^\r\n"]+$')


@dataclass(frozen=True, slots=True)
class JLCOpenApiSettings:
    app_id: str = ""
    access_key: str = ""
    secret_key: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "JLCOpenApiSettings":
        values = os.environ if env is None else env
        return cls(
            app_id=str(values.get(APP_ID_ENV, "")).strip(),
            access_key=str(values.get(ACCESS_KEY_ENV, "")).strip(),
            secret_key=str(values.get(SECRET_KEY_ENV, "")).strip(),
        )

    @property
    def configured(self) -> bool:
        return not self.missing_variables

    @property
    def missing_variables(self) -> list[str]:
        missing: list[str] = []
        if not self.app_id:
            missing.append(APP_ID_ENV)
        if not self.access_key:
            missing.append(ACCESS_KEY_ENV)
        if not self.secret_key:
            missing.append(SECRET_KEY_ENV)
        return missing

    def require_complete(self) -> None:
        if self.missing_variables:
            raise ValueError(
                "missing JLC Open Platform environment variables: "
                + ", ".join(self.missing_variables)
            )
        for label, value in (("app_id", self.app_id), ("access_key", self.access_key)):
            if not _HEADER_VALUE_RE.fullmatch(value):
                raise ValueError(f"invalid {label}")


def compact_json(payload: Any) -> str:
    """Serialize exactly once for both signing and transmission."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _request_path(path: str) -> str:
    value = str(path or "")
    if not value.startswith("/") or "://" in value or "\r" in value or "\n" in value:
        raise ValueError("request path must be an absolute path without a domain")
    return value


def make_nonce() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


def signing_text(
    path: str,
    body: str,
    *,
    timestamp: int,
    nonce: str,
    method: str = "POST",
) -> str:
    method = str(method).upper()
    if method != "POST":
        raise ValueError("JLC Open Platform currently documents POST requests only")
    path = _request_path(path)
    if not _NONCE_RE.fullmatch(nonce):
        raise ValueError("nonce must contain exactly 32 ASCII letters or digits")
    return f"{method}\n{path}\n{int(timestamp)}\n{nonce}\n{body}\n"


def signature(
    secret_key: str,
    path: str,
    body: str,
    *,
    timestamp: int,
    nonce: str,
) -> str:
    canonical = signing_text(path, body, timestamp=timestamp, nonce=nonce)
    digest = hmac.new(
        secret_key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def authorization_header(
    settings: JLCOpenApiSettings,
    path: str,
    body: str,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> str:
    settings.require_complete()
    request_timestamp = int(time.time()) if timestamp is None else int(timestamp)
    request_nonce = make_nonce() if nonce is None else nonce
    signed = signature(
        settings.secret_key,
        path,
        body,
        timestamp=request_timestamp,
        nonce=request_nonce,
    )
    return (
        f'JOP appid="{settings.app_id}",accesskey="{settings.access_key}",'
        f'nonce="{request_nonce}",timestamp="{request_timestamp}",signature="{signed}"'
    )


def signing_self_test() -> bool:
    """Check the signer against the official documentation vector."""
    body = compact_json(
        {"goodsId": 100, "quantity": 52, "createdTime": "2024-03-21 10:03:20"}
    )
    actual = signature(
        "z0BWlikshimuyiwBsH1i2qwnzMb3j3kA",
        "/order/v1/createOrder",
        body,
        timestamp=1625208260,
        nonce="IZHEJYNIHYZIE8S0LLC0VWTPJVRRTO50",
    )
    return hmac.compare_digest(
        actual, "sygwKhKBkLwHVv0c7D+a/A7JTEJjGH/kLugFKh16918="
    )


__all__ = [
    "ACCESS_KEY_ENV",
    "APP_ID_ENV",
    "JLC_OPEN_API_BASE_URL",
    "JLCOpenApiSettings",
    "SECRET_KEY_ENV",
    "authorization_header",
    "compact_json",
    "make_nonce",
    "signature",
    "signing_self_test",
    "signing_text",
]
