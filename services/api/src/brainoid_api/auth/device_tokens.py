"""Dedicated RSA device tokens and authenticated, short-lived encrypted receipts."""

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import jwt
from brainoid_contracts import ResourceId, Version
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from brainoid_api.errors import APIError, ErrorCode


class DeviceIdentity(BaseModel):
    """Cryptographically verified identity; repository must still check current DB/ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    iss: str
    aud: str
    sub: ResourceId
    tenant_id: ResourceId
    token_use: Literal["device_access"]
    authorization_version: Version
    connection_generation: Version
    credential_epoch: ResourceId
    family_id: ResourceId
    iat: Annotated[int, Field(strict=True, ge=0)]
    exp: Annotated[int, Field(strict=True, ge=0)]


class DeviceTokens:
    def __init__(
        self,
        keyring: str,
        *,
        project: str,
        credential_epoch: ResourceId,
        test_only: bool,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        try:
            raw = json.loads(keyring)
            if raw["test_only"] is not test_only:
                raise ValueError()
            self._kid = raw["active_signing_kid"]
            self._receipt_kid = raw["active_receipt_kid"]
            private = serialization.load_pem_private_key(
                raw["signing_private_key"].encode(), password=None
            )
            if not isinstance(private, rsa.RSAPrivateKey) or private.key_size < 2048:
                raise ValueError()
            self._private = private
            self._public: dict[str, rsa.RSAPublicKey] = {}
            for kid, pem in raw["verification_keys"].items():
                public = serialization.load_pem_public_key(pem.encode())
                if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 2048:
                    raise ValueError()
                self._public[kid] = public
            if (
                self._public[self._kid].public_numbers()
                != self._private.public_key().public_numbers()
            ):
                raise ValueError()
            self._receipts = {
                kid: base64.b64decode(key, validate=True)
                for kid, key in raw["receipt_keys"].items()
            }
            self._lookup = base64.b64decode(raw["lookup_key"], validate=True)
            if len(self._lookup) != 32 or any(len(key) != 32 for key in self._receipts.values()):
                raise ValueError()
            if self._receipt_kid not in self._receipts or not self._public:
                raise ValueError()
            if any(
                not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", kid)
                for kid in (*self._public, *self._receipts)
            ):
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("Invalid device keyring") from None
        self.epoch = credential_epoch
        self._issuer = f"urn:brainoid:{project}:device"
        self._audience = f"urn:brainoid:{project}:device-api"
        self._now = now

    @classmethod
    def from_file(
        cls, path: str, *, project: str, credential_epoch: ResourceId, test_only: bool
    ) -> "DeviceTokens":
        source = Path(path)
        if not test_only and source.stat().st_mode & 0o077:
            raise ValueError("Device keyring must only be accessible to its service user")
        return cls(
            source.read_text(),
            project=project,
            credential_epoch=credential_epoch,
            test_only=test_only,
        )

    def digest(self, purpose: str, value: str) -> str:
        return hmac.new(self._lookup, (purpose + "\0" + value).encode(), hashlib.sha256).hexdigest()

    def issue(
        self,
        *,
        tenant_id: ResourceId,
        device_id: ResourceId,
        family_id: ResourceId,
        authorization_version: int,
        connection_generation: int,
    ) -> str:
        now = int(self._now().timestamp())
        claims = DeviceIdentity(
            iss=self._issuer,
            aud=self._audience,
            sub=device_id,
            tenant_id=tenant_id,
            token_use="device_access",
            authorization_version=authorization_version,
            connection_generation=connection_generation,
            credential_epoch=self.epoch,
            family_id=family_id,
            iat=now,
            exp=now + 900,
        )
        return jwt.encode(
            claims.model_dump(mode="json"),
            self._private,
            algorithm="RS256",
            headers={"kid": self._kid, "typ": "device+jwt"},
        )

    def verify(self, token: str) -> DeviceIdentity:
        try:
            if len(token) > 16384:
                raise ValueError()
            header = jwt.get_unverified_header(token)
            if (
                set(header) != {"alg", "kid", "typ"}
                or header["alg"] != "RS256"
                or header["typ"] != "device+jwt"
            ):
                raise ValueError()
            claims = DeviceIdentity.model_validate(
                jwt.decode(
                    token,
                    self._public[header["kid"]],
                    algorithms=["RS256"],
                    issuer=self._issuer,
                    audience=self._audience,
                    options={
                        "verify_exp": False,
                        "verify_iat": False,
                        "verify_nbf": False,
                        "strict_aud": True,
                    },
                )
            )
            if (
                claims.credential_epoch != self.epoch
                or claims.exp - claims.iat != 900
                or not claims.iat <= self._now().timestamp() < claims.exp
            ):
                raise ValueError()
            return claims
        except (jwt.PyJWTError, ValidationError, ValueError, TypeError, KeyError):
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED) from None

    def seal(self, plaintext: str, binding: str) -> dict[str, str]:
        nonce = secrets.token_bytes(12)
        data = AESGCM(self._receipts[self._receipt_kid]).encrypt(
            nonce, plaintext.encode(), binding.encode()
        )
        return {"kid": self._receipt_kid, "data": base64.b64encode(nonce + data).decode()}

    def open(self, receipt: dict[str, str], binding: str) -> str:
        try:
            data = base64.b64decode(receipt["data"], validate=True)
            return (
                AESGCM(self._receipts[receipt["kid"]])
                .decrypt(data[:12], data[12:], binding.encode())
                .decode()
            )
        except (KeyError, ValueError, TypeError, InvalidTag):
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
