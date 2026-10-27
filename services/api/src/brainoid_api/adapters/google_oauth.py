"""Google protocol adapter with an explicit transport; no network/default credentials."""

from collections.abc import Callable
from urllib.parse import urlencode, urlsplit

from brainoid_memory.calendar_oauth import Grant, SecretUnavailable, challenge
from pydantic import SecretStr


class GoogleOAuth:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: SecretStr,
        redirect_uri: str,
        post: Callable[[str, dict[str, str]], dict[str, object]],
    ) -> None:
        url = urlsplit(redirect_uri)
        if (
            not client_id
            or not client_secret.get_secret_value()
            or url.scheme != "https"
            or not url.hostname
            or url.query
            or url.fragment
            or url.username
        ):
            raise ValueError("Explicit registered HTTPS OAuth configuration required")
        self.client_id, self.client_secret = client_id, client_secret
        self.redirect_uri, self.post = redirect_uri, post

    def authorization_url(self, *, state: str, verifier: str, scopes: tuple[str, ...]) -> str:
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": challenge(verifier),
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
        )

    def exchange(self, *, code: str, verifier: str) -> Grant:
        try:
            result = self.post(
                "https://oauth2.googleapis.com/token",
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret.get_secret_value(),
                    "redirect_uri": self.redirect_uri,
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                },
            )
            access, refresh, scopes = (
                result.get("access_token"),
                result.get("refresh_token"),
                result.get("scope"),
            )
            if result.get("token_type") != "Bearer" or not all(
                isinstance(v, str) and v for v in (access, refresh, scopes)
            ):
                raise SecretUnavailable()
            assert isinstance(access, str) and isinstance(refresh, str) and isinstance(scopes, str)
            return Grant(
                access_token=SecretStr(access),
                refresh_token=SecretStr(refresh),
                scopes=frozenset(scopes.split()),
            )
        except Exception:
            # An uncertain exchange is never retried by this adapter.
            raise SecretUnavailable() from None
