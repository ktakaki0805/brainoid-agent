"""Firebase Admin verification with explicit project, revocation and safe errors."""

import os
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Protocol

from firebase_admin import App, auth, exceptions  # type: ignore[import-untyped]
from google.auth.exceptions import GoogleAuthError
from requests.exceptions import RequestException

from brainoid_api.auth.context import AuthenticatedIdentity
from brainoid_api.errors import APIError, ErrorCode
from brainoid_api.settings import ConfigurationError, Settings


class IdentityVerifier(Protocol):
    def verify(self, token: str) -> AuthenticatedIdentity: ...


def validate_sdk_environment(settings: Settings) -> None:
    """SDKs read process environment even when the app receives explicit Settings."""
    if "STORAGE_EMULATOR_HOST" in os.environ:
        raise ConfigurationError("Storage emulator overrides are not supported")
    for name, expected in (
        ("FIREBASE_AUTH_EMULATOR_HOST", settings.firebase_auth_emulator_host),
        ("FIRESTORE_EMULATOR_HOST", settings.firestore_emulator_host),
    ):
        if os.environ.get(name) != expected:
            raise ConfigurationError("SDK emulator environment must match API settings")
    for name in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT"):
        if name in os.environ and os.environ[name] != settings.gcp_project_id:
            raise ConfigurationError("SDK project environment must match API settings")


def _identity(claims: Mapping[str, Any]) -> AuthenticatedIdentity:
    """The emulator skips time checks; enforce the same strict times in every environment."""
    firebase = claims.get("firebase", {})
    # ADR-005 uses global Firebase UIDs. The root Admin SDK also verifies Auth tenant
    # tokens, whose UIDs are in a separate namespace; never resolve those as root users.
    if not isinstance(firebase, dict) or "tenant" in firebase:
        raise ValueError("Unsupported Firebase identity namespace")
    times: dict[str, datetime] = {}
    for claim in ("auth_time", "iat", "exp"):
        value = claims.get(claim)
        if type(value) is not int or value < 0:
            raise ValueError("Invalid authentication timestamp")
        times[claim] = datetime.fromtimestamp(value, UTC)
    now = datetime.now(UTC)
    if not times["auth_time"] <= times["iat"] <= now < times["exp"]:
        raise ValueError("Invalid authentication lifetime")
    return AuthenticatedIdentity(
        user_id=claims["uid"],
        authenticated_at=times["auth_time"],
        issued_at=times["iat"],
        expires_at=times["exp"],
    )


class FirebaseIdentityVerifier:
    def __init__(self, app: App, settings: Settings) -> None:
        if settings.auth_provider != "firebase" or app.project_id != settings.gcp_project_id:
            raise ConfigurationError("Firebase authentication requires the configured project")
        validate_sdk_environment(settings)
        self._app = app
        self._settings = settings

    def verify(self, token: str) -> AuthenticatedIdentity:
        try:
            validate_sdk_environment(self._settings)
            claims = auth.verify_id_token(
                token, app=self._app, check_revoked=True, clock_skew_seconds=0
            )
            identity = _identity(claims)
            # Admin 7.7.0 checks iat for revocation. Also check auth_time so a token
            # refreshed after revocation cannot reuse an older authentication session.
            user = auth.get_user(identity.user_id, app=self._app)
            if (
                user.disabled
                or identity.authenticated_at.timestamp() * 1000 < user.tokens_valid_after_timestamp
            ):
                raise APIError(ErrorCode.AUTHENTICATION_REQUIRED)
            validate_sdk_environment(self._settings)
            return identity
        except (
            auth.InvalidIdTokenError,
            auth.UserDisabledError,
            auth.UserNotFoundError,
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
        ):
            # SDK exception messages can contain the raw JWT or attacker-controlled claims.
            raise APIError(ErrorCode.AUTHENTICATION_REQUIRED) from None
        except (ConfigurationError, exceptions.FirebaseError, GoogleAuthError, RequestException):
            raise APIError(ErrorCode.TEMPORARILY_UNAVAILABLE) from None
