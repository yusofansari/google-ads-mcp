"""ASGI wrapper that adds CORS headers and rewrites paths for nginx proxy."""

from starlette.responses import Response


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Expose-Headers": "*",
}


def create_app(base_app, service_prefix: str):
    """Create an ASGI app that wraps the Starlette MCP app.

    Handles:
    - Path rewriting: /mcp/SERVICE/authorize → /authorize
    - Well-known path rewriting
    - CORS headers on all responses
    - OPTIONS preflight
    - GET health check (non-SSE)

    Args:
        base_app: The Starlette app from FastMCP.streamable_http_app()
        service_prefix: e.g. "/mcp/gads"
    """

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            await base_app(scope, receive, send)
            return

        if scope["type"] == "http":
            method = scope.get("method", "")
            path = scope.get("path", "")

            # Handle CORS preflight
            if method == "OPTIONS":
                response = Response("", status_code=204, headers=CORS_HEADERS)
                await response(scope, receive, send)
                return

            # Handle GET without Accept: text/event-stream (health check)
            if method == "GET" and path == service_prefix:
                accept = ""
                for key, val in scope.get("headers", []):
                    if key == b"accept":
                        accept = val.decode()
                        break
                if "text/event-stream" not in accept:
                    response = Response("ok", status_code=200, headers=CORS_HEADERS)
                    await response(scope, receive, send)
                    return

            # Path rewriting for OAuth sub-routes
            # /mcp/gads/authorize → /authorize
            # /mcp/gads/token → /token
            # /mcp/gads/register → /register
            # /mcp/gads/callback → /callback
            if path.startswith(service_prefix + "/"):
                scope = dict(scope)
                scope["path"] = path[len(service_prefix):]

            # Well-known path rewriting
            # /.well-known/oauth-authorization-server/mcp/gads →
            #     /.well-known/oauth-authorization-server
            well_known_as = f"/.well-known/oauth-authorization-server{service_prefix}"
            if path == well_known_as:
                scope = dict(scope)
                scope["path"] = "/.well-known/oauth-authorization-server"

            # Add CORS headers to all responses
            original_send = send

            async def send_with_cors(message):
                if message["type"] == "http.response.start":
                    headers = list(message.get("headers", []))
                    for key, val in CORS_HEADERS.items():
                        headers.append((key.lower().encode(), val.encode()))
                    message = dict(message)
                    message["headers"] = headers
                await original_send(message)

            await base_app(scope, receive, send_with_cors)
            return

        await base_app(scope, receive, send)

    return app
