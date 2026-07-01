"""
OAuthAuthorizationServerProvider that proxies to Google OAuth.

This implements the MCP OAuth interface. The MCP server acts as an OAuth
authorization server to Claude.ai, and internally uses Google OAuth to
authenticate users.
"""

import secrets
import time
import urllib.parse

from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from shared import database as db
from shared.config import (
    GOOGLE_AUTH_URI,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_SCOPES,
    GOOGLE_TOKEN_URI,
    BASE_URL,
)

# Token lifetime
ACCESS_TOKEN_TTL = 3600 * 24      # 24 hours
REFRESH_TOKEN_TTL = 3600 * 24 * 90  # 90 days
AUTH_CODE_TTL = 300                 # 5 minutes


def _gen_token(nbytes=32) -> str:
    return secrets.token_urlsafe(nbytes)


class GoogleOAuthProvider(OAuthAuthorizationServerProvider):
    """Proxies MCP OAuth to Google OAuth for a specific service."""

    def __init__(self, service: str):
        """
        Args:
            service: one of 'gads', 'ga4', 'gsc'
        """
        self.service = service
        self.google_scopes = GOOGLE_SCOPES[service]
        self.callback_url = f"{BASE_URL}/mcp/{service}/callback"

    # ── Client Registration ─────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        row = db.get_client(client_id)
        if not row:
            return None
        return OAuthClientInformationFull(
            client_id=row["client_id"],
            client_secret=row["client_secret"],
            redirect_uris=row["redirect_uris"],
            client_name=row["client_name"],
            grant_types=row["grant_types"],
            response_types=row["response_types"],
            token_endpoint_auth_method=row["token_endpoint_auth_method"],
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        db.save_client(
            client_id=client_info.client_id,
            client_secret=client_info.client_secret,
            redirect_uris=[str(u) for u in (client_info.redirect_uris or [])],
            client_name=client_info.client_name or "",
            grant_types=client_info.grant_types or ["authorization_code"],
            response_types=client_info.response_types or ["code"],
            token_endpoint_auth_method=client_info.token_endpoint_auth_method or "client_secret_post",
        )

    # ── Authorization ───────────────────────────────────────────────

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: AuthorizationParams,
    ) -> str:
        google_state = _gen_token()

        db.create_auth_session(
            google_state=google_state,
            mcp_state=params.state,
            code_challenge=params.code_challenge,
            mcp_client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided=params.redirect_uri_provided_explicitly,
            service=self.service,
            scopes=params.scopes,
            resource=params.resource,
        )

        # Build Google OAuth URL
        google_params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": self.callback_url,
            "response_type": "code",
            "scope": " ".join(self.google_scopes),
            "state": google_state,
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{GOOGLE_AUTH_URI}?{urllib.parse.urlencode(google_params)}"

    # ── Authorization Code ──────────────────────────────────────────

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> AuthorizationCode | None:
        session = db.get_auth_session_by_code(authorization_code)
        if not session:
            return None
        if session["code_used"]:
            return None

        return AuthorizationCode(
            code=authorization_code,
            scopes=self.google_scopes,
            expires_at=session["created_at"] + AUTH_CODE_TTL,
            client_id=session["mcp_client_id"],
            code_challenge=session["code_challenge"],
            redirect_uri=session["redirect_uri"],
            redirect_uri_provided_explicitly=bool(session["redirect_uri_provided"]),
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        session = db.get_auth_session_by_code(authorization_code.code)
        if not session:
            raise ValueError("Invalid authorization code")

        db.mark_code_used(authorization_code.code)

        now = time.time()
        access_token = _gen_token()
        refresh_token = _gen_token()

        # Save access token
        db.save_mcp_token(
            token=access_token,
            token_type="access",
            mcp_client_id=client.client_id,
            user_email=session.get("user_email"),
            google_refresh_token=session["google_refresh_token"],
            scopes=self.google_scopes,
            service=self.service,
            expires_at=now + ACCESS_TOKEN_TTL,
        )

        # Save refresh token
        db.save_mcp_token(
            token=refresh_token,
            token_type="refresh",
            mcp_client_id=client.client_id,
            user_email=session.get("user_email"),
            google_refresh_token=session["google_refresh_token"],
            scopes=self.google_scopes,
            service=self.service,
            expires_at=now + REFRESH_TOKEN_TTL,
        )

        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token,
            scope=" ".join(self.google_scopes),
        )

    # ── Refresh Token ───────────────────────────────────────────────

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> AuthorizationCode | None:
        row = db.get_mcp_token(refresh_token)
        if not row or row["token_type"] != "refresh":
            return None
        return AuthorizationCode(
            code=refresh_token,
            scopes=row["scopes"],
            expires_at=row["expires_at"] or (time.time() + REFRESH_TOKEN_TTL),
            client_id=row["mcp_client_id"],
            code_challenge="",
            redirect_uri="https://placeholder.invalid",
            redirect_uri_provided_explicitly=False,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token,
        scopes: list[str],
    ) -> OAuthToken:
        old_row = db.get_mcp_token(refresh_token.code)
        if not old_row:
            raise ValueError("Invalid refresh token")

        # Revoke old tokens
        db.revoke_tokens_for(token=refresh_token.code)

        now = time.time()
        new_access = _gen_token()
        new_refresh = _gen_token()

        db.save_mcp_token(
            token=new_access,
            token_type="access",
            mcp_client_id=client.client_id,
            user_email=old_row.get("user_email"),
            google_refresh_token=old_row["google_refresh_token"],
            scopes=scopes or old_row["scopes"],
            service=self.service,
            expires_at=now + ACCESS_TOKEN_TTL,
        )

        db.save_mcp_token(
            token=new_refresh,
            token_type="refresh",
            mcp_client_id=client.client_id,
            user_email=old_row.get("user_email"),
            google_refresh_token=old_row["google_refresh_token"],
            scopes=scopes or old_row["scopes"],
            service=self.service,
            expires_at=now + REFRESH_TOKEN_TTL,
        )

        return OAuthToken(
            access_token=new_access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=new_refresh,
            scope=" ".join(scopes or old_row["scopes"]),
        )

    # ── Access Token Verification ───────────────────────────────────

    async def load_access_token(self, token: str):
        from mcp.server.auth.middleware.auth_context import AccessToken

        row = db.get_mcp_token(token)
        if not row or row["token_type"] != "access":
            return None
        if row.get("expires_at") and row["expires_at"] < time.time():
            return None

        return AccessToken(
            token=token,
            client_id=row["mcp_client_id"],
            scopes=row["scopes"],
            expires_at=int(row["expires_at"]) if row.get("expires_at") else None,
        )

    # ── Revocation ──────────────────────────────────────────────────

    async def revoke_token(self, token) -> None:
        if hasattr(token, "code"):
            db.revoke_tokens_for(token=token.code)
        elif hasattr(token, "token"):
            db.revoke_tokens_for(token=token.token)
