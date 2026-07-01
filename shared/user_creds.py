"""Per-request user credential helpers.

Tool handlers call these to get the current user's Google credentials.
"""

from google.oauth2.credentials import Credentials

from mcp.server.auth.middleware.auth_context import get_access_token

from shared.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_TOKEN_URI
from shared.database import get_google_creds_by_mcp_token


def get_current_google_refresh_token() -> str:
    """Get the current user's Google refresh token from the MCP access token."""
    access_token = get_access_token()
    if not access_token:
        raise PermissionError(
            "Not authenticated. Please reconnect the MCP server to authorize."
        )
    creds = get_google_creds_by_mcp_token(access_token.token)
    if not creds or not creds.get("refresh_token"):
        raise PermissionError(
            "No Google credentials found. Please re-authorize the MCP server."
        )
    return creds["refresh_token"]


def get_current_google_credentials(scopes: list[str] | None = None) -> Credentials:
    """Build a google.oauth2.credentials.Credentials for the current user."""
    refresh_token = get_current_google_refresh_token()
    return Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri=GOOGLE_TOKEN_URI,
        scopes=scopes,
    )
