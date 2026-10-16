"""Google Sign-In for the account gateway: the authorization-code flow with PKCE.

Inert until an administrator enters the OAuth client's id and secret and the
server's public address in the admin page (Google redirects back to
``<public address>/_hub/google/callback``, which has to be listed among the
client's authorized redirect URIs in the Google Cloud console).

The ID token Google returns is verified here, not trusted: its signature
against Google's published keys, the audience (this client), the issuer, the
expiry, the nonce this flow sent, and that Google verified the e-mail.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from raven.accounts.settings import HubSettings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a credential
CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
CALLBACK_PATH = "/_hub/google/callback"
PENDING_TTL_S = 600


class GoogleSignInError(Exception):
    """A sign-in that cannot complete, in words a person can act on."""


@dataclass
class Identity:
    sub: str
    email: str
    name: str
    link_account: str | None = None


@dataclass
class _Pending:
    verifier: str
    nonce: str
    created: float
    link_account: str | None


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


Exchange = Callable[[dict[str, str]], Awaitable[dict[str, Any]]]
KeyFor = Callable[[str], Any]


async def _exchange(form: dict[str, str]) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(TOKEN_URL, data=form, headers={"Accept": "application/json"})
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        raise GoogleSignInError(f"Google refused the sign-in ({body.get('error') or response.status_code})")
    return body


_jwks_client: Any = None


def _google_key(token: str) -> Any:
    global _jwks_client
    import jwt

    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(CERTS_URL, cache_keys=True, lifespan=3600)
    return _jwks_client.get_signing_key_from_jwt(token).key


class GoogleSignIn:
    def __init__(
        self, settings: HubSettings, *, exchange: Exchange | None = None, key_for: KeyFor | None = None
    ) -> None:
        self.settings = settings
        self._exchange = exchange or _exchange
        self._key_for = key_for or _google_key
        self._pending: dict[str, _Pending] = {}

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_url.rstrip('/')}{CALLBACK_PATH}"

    def start(self, *, link_account: str | None = None, now: float | None = None) -> tuple[str, str]:
        """The Google URL to send the browser to, and the state that identifies this attempt."""
        if not self.settings.google_ready:
            raise GoogleSignInError("Google Sign-In is not set up on this server yet")
        now = time.time() if now is None else now
        self._pending = {k: v for k, v in self._pending.items() if now - v.created < PENDING_TTL_S}
        state, verifier, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(48), secrets.token_urlsafe(16)
        self._pending[state] = _Pending(verifier, nonce, now, link_account)
        query = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _challenge(verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{AUTH_URL}?{urlencode(query)}", state

    async def finish(self, state: str, code: str, *, now: float | None = None) -> Identity:
        now = time.time() if now is None else now
        pending = self._pending.pop(state or "", None)
        if pending is None or now - pending.created >= PENDING_TTL_S:
            raise GoogleSignInError("this sign-in link has expired or was already used; start again")
        if not code:
            raise GoogleSignInError("Google sent no authorization code back")
        tokens = await self._exchange(
            {
                "code": code,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": pending.verifier,
            }
        )
        claims = self.verify(str(tokens.get("id_token") or ""), pending.nonce)
        return Identity(
            sub=str(claims["sub"]),
            email=str(claims.get("email") or "").lower(),
            name=str(claims.get("name") or ""),
            link_account=pending.link_account,
        )

    def verify(self, id_token: str, nonce: str) -> dict[str, Any]:
        import jwt

        if not id_token:
            raise GoogleSignInError("Google sent no ID token back")
        try:
            claims = jwt.decode(
                id_token,
                self._key_for(id_token),
                algorithms=["RS256"],
                audience=self.settings.google_client_id,
                issuer=list(ISSUERS),
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
                leeway=60,
            )
        except jwt.PyJWTError as exc:
            raise GoogleSignInError(f"Google's ID token did not verify ({exc})") from exc
        if claims.get("nonce") != nonce:
            raise GoogleSignInError("Google's ID token belongs to another sign-in attempt")
        if not claims.get("email_verified"):
            raise GoogleSignInError("Google has not verified this account's e-mail address")
        return claims


__all__ = ["CALLBACK_PATH", "GoogleSignIn", "GoogleSignInError", "Identity"]
