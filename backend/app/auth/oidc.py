"""OpenID Connect login (authorization code flow with PKCE).

* state, nonce and PKCE verifier live in a short-lived HMAC-signed cookie (no server state),
* the ID token is verified against the provider's JWKS (signature, issuer, audience, expiry,
  nonce),
* the dashboard role is derived from IdP groups on every login,
* MFA is the identity provider's job, so OIDC users skip the dashboard's TOTP step.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKSet

from app.config import Settings

STATE_COOKIE = "pvd_oidc"
STATE_TTL = 600
DISCOVERY_TTL = 3600


class OidcError(Exception):
    pass


@dataclass
class OidcIdentity:
    sub: str
    username: str
    email: str | None
    groups: list[str]
    role: str | None  # None = no access


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _groups(value: str) -> set[str]:
    return {g.strip() for g in value.split(",") if g.strip()}


def role_from_groups(settings: Settings, groups: list[str]) -> str | None:
    have = set(groups)
    if have & _groups(settings.oidc_admin_groups):
        return "admin"
    if have & _groups(settings.oidc_operator_groups):
        return "operator"
    viewers = _groups(settings.oidc_viewer_groups)
    if not viewers or have & viewers:
        return "viewer"
    return None


class OidcClient:
    def __init__(self, settings: Settings, app_key: str,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._key = hashlib.sha256(b"pve-dashboard/oidc-state/v1" + app_key.encode()).digest()
        self._http = httpx.AsyncClient(timeout=10, transport=transport)
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self._jwks: PyJWKSet | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # --- provider metadata ----------------------------------------------------------------------

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None or time.time() - self._discovery_at > DISCOVERY_TTL:
            url = self.s.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
            try:
                resp = await self._http.get(url)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise OidcError(f"Discovery von {url} fehlgeschlagen: {exc}") from exc
            self._discovery = resp.json()
            self._discovery_at = time.time()
            self._jwks = None
        return self._discovery

    async def _jwk_set(self, refresh: bool = False) -> PyJWKSet:
        if self._jwks is None or refresh:
            resp = await self._http.get((await self.discovery())["jwks_uri"])
            resp.raise_for_status()
            self._jwks = PyJWKSet.from_dict(resp.json())
        return self._jwks

    # --- state cookie -------------------------------------------------------------------------

    def _sign(self, payload: dict[str, Any]) -> str:
        body = _b64(json.dumps(payload, separators=(",", ":")).encode())
        sig = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        return f"{body}.{sig}"

    def _unsign(self, token: str) -> dict[str, Any]:
        try:
            body, sig = token.split(".", 1)
        except ValueError as exc:
            raise OidcError("Ungültiger Login-Status") from exc
        good = _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            raise OidcError("Login-Status manipuliert")
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if data.get("exp", 0) < time.time():
            raise OidcError("Login hat zu lange gedauert – bitte erneut versuchen")
        return data

    # --- flow ---------------------------------------------------------------------------------

    async def start(self, redirect_uri: str, next_path: str) -> tuple[str, str]:
        """Returns (authorization URL, signed state cookie)."""
        meta = await self.discovery()
        state, nonce, verifier = (secrets.token_urlsafe(24), secrets.token_urlsafe(24),
                                  secrets.token_urlsafe(48))
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        params = {"response_type": "code", "client_id": self.s.oidc_client_id,
                  "redirect_uri": redirect_uri, "scope": self.s.oidc_scopes, "state": state,
                  "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256"}
        cookie = self._sign({"state": state, "nonce": nonce, "verifier": verifier,
                             "next": next_path, "redirect_uri": redirect_uri,
                             "exp": int(time.time()) + STATE_TTL})
        return f"{meta['authorization_endpoint']}?{urlencode(params)}", cookie

    async def finish(self, code: str, state: str, cookie: str | None) -> tuple[OidcIdentity, str]:
        """Exchange the code, verify the ID token. Returns (identity, next path)."""
        if not cookie:
            raise OidcError("Login-Status fehlt (Cookies blockiert?)")
        st = self._unsign(cookie)
        if not hmac.compare_digest(st["state"], state):
            raise OidcError("State stimmt nicht überein")
        meta = await self.discovery()
        data = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": st["redirect_uri"], "code_verifier": st["verifier"],
                "client_id": self.s.oidc_client_id}
        secret = self.s.oidc_client_secret.get_secret_value()
        auth = (self.s.oidc_client_id, secret) if secret else None
        try:
            resp = await self._http.post(meta["token_endpoint"], data=data, auth=auth)
        except httpx.HTTPError as exc:
            raise OidcError(f"Token-Endpunkt nicht erreichbar: {exc}") from exc
        if resp.status_code != 200:
            raise OidcError(f"Token-Abruf abgelehnt ({resp.status_code}): {resp.text[:200]}")
        tokens = resp.json()
        claims = await self._verify(tokens.get("id_token", ""), st["nonce"], meta)
        # Some IdPs only put groups into the userinfo response.
        if self.s.oidc_groups_claim not in claims and tokens.get("access_token") and \
                meta.get("userinfo_endpoint"):
            info = await self._http.get(meta["userinfo_endpoint"], headers={
                "Authorization": f"Bearer {tokens['access_token']}"})
            if info.status_code == 200 and info.json().get("sub") == claims["sub"]:
                claims = {**info.json(), **claims, self.s.oidc_groups_claim:
                          info.json().get(self.s.oidc_groups_claim, [])}
        groups = claims.get(self.s.oidc_groups_claim) or []
        if isinstance(groups, str):
            groups = [groups]
        username = str(claims.get(self.s.oidc_username_claim) or claims.get("email")
                       or claims["sub"])[:64]
        ident = OidcIdentity(sub=str(claims["sub"]), username=username,
                             email=claims.get("email"), groups=[str(g) for g in groups],
                             role=role_from_groups(self.s, [str(g) for g in groups]))
        next_path = st.get("next") or "/"
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"
        return ident, next_path

    async def _verify(self, id_token: str, nonce: str, meta: dict[str, Any]) -> dict[str, Any]:
        if not id_token:
            raise OidcError("Kein ID-Token erhalten")
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
            jwks = await self._jwk_set()
            try:
                key = jwks[kid] if kid else jwks.keys[0]
            except KeyError:
                key = (await self._jwk_set(refresh=True))[kid]  # key rotation
            claims = jwt.decode(id_token, key=key.key, algorithms=["RS256", "ES256", "PS256",
                                                                   "EdDSA"],
                                audience=self.s.oidc_client_id, issuer=meta["issuer"],
                                leeway=60, options={"require": ["exp", "iat", "sub"]})
        except (jwt.PyJWTError, KeyError, IndexError) as exc:
            raise OidcError(f"ID-Token ungültig: {exc}") from exc
        if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("Nonce stimmt nicht überein")
        return claims
