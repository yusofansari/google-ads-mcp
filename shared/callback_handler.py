"""Google OAuth callback handler.

After the user authorizes on Google, Google redirects here.
We exchange the code for tokens, store them, and redirect back to
Claude.ai with an MCP authorization code.
"""

import secrets
import urllib.parse

import httpx
from starlette.requests import Request
from starlette.responses import RedirectResponse, JSONResponse

from shared import database as db
from shared.config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_TOKEN_URI


async def handle_google_callback(request: Request, callback_url: str) -> RedirectResponse | JSONResponse:
    """Process the Google OAuth callback.

    Args:
        request: The Starlette request.
        callback_url: The full callback URL (used as redirect_uri for token exchange).
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return JSONResponse(
            {"error": error, "description": request.query_params.get("error_description", "")},
            status_code=400,
        )

    if not code or not state:
        return JSONResponse({"error": "missing code or state"}, status_code=400)

    # Look up the auth session
    session = db.get_auth_session_by_google_state(state)
    if not session:
        return JSONResponse({"error": "invalid state"}, status_code=400)

    # Exchange Google auth code for tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URI,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": callback_url,
                "grant_type": "authorization_code",
            },
        )

    if resp.status_code != 200:
        return JSONResponse(
            {"error": "google_token_exchange_failed", "details": resp.text},
            status_code=502,
        )

    token_data = resp.json()
    google_access_token = token_data.get("access_token", "")
    google_refresh_token = token_data.get("refresh_token", "")

    # Get user email from Google
    user_email = ""
    if google_access_token:
        async with httpx.AsyncClient() as client:
            info = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {google_access_token}"},
            )
            if info.status_code == 200:
                user_email = info.json().get("email", "")

    # Generate MCP authorization code
    mcp_auth_code = secrets.token_urlsafe(32)

    # Store everything
    db.complete_auth_session(
        google_state=state,
        mcp_auth_code=mcp_auth_code,
        google_refresh_token=google_refresh_token,
        google_access_token=google_access_token,
        user_email=user_email,
    )

    # Redirect back to Claude.ai with the MCP auth code
    redirect_uri = session["redirect_uri"]
    params = {"code": mcp_auth_code}
    if session.get("mcp_state"):
        params["state"] = session["mcp_state"]

    separator = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{separator}{urllib.parse.urlencode(params)}",
        status_code=302,
    )
