"""
Entry point for the public Google Ads MCP server.
Configures OAuth, ASGI wrapper, and launches uvicorn.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.config import BASE_URL
from shared.database import init_db
from shared.google_oauth_provider import GoogleOAuthProvider
from shared.callback_handler import handle_google_callback
from shared.asgi_wrapper import create_app

# Initialize database
init_db()

SERVICE = "gads"
SERVICE_PREFIX = f"/mcp/{SERVICE}"
PORT = 8102

# Import the server module (triggers tool registration)
import google_ads.server as server  # noqa: E402

# Configure MCP with OAuth
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

server.mcp.settings.streamable_http_path = SERVICE_PREFIX
server.mcp.settings.json_response = True
server.mcp.settings.stateless_http = True
server.mcp.settings.transport_security.enable_dns_rebinding_protection = False

provider = GoogleOAuthProvider(SERVICE)
server.mcp._auth_server_provider = provider

# Create token verifier from provider
from mcp.server.auth.provider import ProviderTokenVerifier

server.mcp._token_verifier = ProviderTokenVerifier(provider)

server.mcp.settings.auth = AuthSettings(
    issuer_url=f"{BASE_URL}{SERVICE_PREFIX}",
    resource_server_url=f"{BASE_URL}{SERVICE_PREFIX}",
    client_registration_options=ClientRegistrationOptions(enabled=True),
)

# Add Google OAuth callback route
CALLBACK_URL = f"{BASE_URL}{SERVICE_PREFIX}/callback"


@server.mcp.custom_route("/callback", methods=["GET"])
async def google_callback(request):
    return await handle_google_callback(request, CALLBACK_URL)


if __name__ == "__main__":
    import uvicorn

    starlette_app = server.mcp.streamable_http_app()
    app = create_app(starlette_app, SERVICE_PREFIX)

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
