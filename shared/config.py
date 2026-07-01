"""Shared configuration for all MCP public services."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / "mcp.db"

# Google OAuth2 client credentials (your app's credentials)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

# Google Ads developer token (app-level, not per-user)
GADS_DEVELOPER_TOKEN = os.environ.get("GADS_DEVELOPER_TOKEN", "")

# Base URL for the public MCP server
BASE_URL = os.environ.get("MCP_BASE_URL", "https://saveyourclicks.com")

# Google OAuth scopes per service
GOOGLE_SCOPES = {
    "gads": ["https://www.googleapis.com/auth/adwords"],
    "ga4": [
        "https://www.googleapis.com/auth/analytics.readonly",
    ],
    "gsc": ["https://www.googleapis.com/auth/webmasters.readonly"],
}

GOOGLE_TOKEN_URI = "https://accounts.google.com/o/oauth2/token"
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
