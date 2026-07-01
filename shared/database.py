"""SQLite database for multi-tenant MCP OAuth state."""

import json
import sqlite3
import threading
from contextlib import contextmanager

from shared.config import DB_PATH

_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


@contextmanager
def db():
    conn = _get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id       TEXT PRIMARY KEY,
                client_secret   TEXT,
                redirect_uris   TEXT,
                client_name     TEXT,
                grant_types     TEXT,
                response_types  TEXT,
                token_endpoint_auth_method TEXT,
                created_at      REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS auth_sessions (
                google_state            TEXT PRIMARY KEY,
                mcp_state               TEXT,
                code_challenge          TEXT,
                code_challenge_method   TEXT DEFAULT 'S256',
                mcp_client_id           TEXT,
                redirect_uri            TEXT,
                redirect_uri_provided   INTEGER DEFAULT 1,
                service                 TEXT,
                scopes                  TEXT,
                resource                TEXT,
                created_at              REAL DEFAULT (unixepoch()),

                -- filled after Google callback
                mcp_auth_code           TEXT UNIQUE,
                google_refresh_token    TEXT,
                google_access_token     TEXT,
                user_email              TEXT,
                code_used               INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS mcp_tokens (
                token           TEXT PRIMARY KEY,
                token_type      TEXT DEFAULT 'access',
                mcp_client_id   TEXT,
                user_email      TEXT,
                google_refresh_token TEXT,
                scopes          TEXT,
                service         TEXT,
                expires_at      REAL,
                created_at      REAL DEFAULT (unixepoch()),
                revoked         INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_tokens_client
                ON mcp_tokens(mcp_client_id, token_type);
            CREATE INDEX IF NOT EXISTS idx_sessions_code
                ON auth_sessions(mcp_auth_code);
        """)


# --- OAuth Clients ---

def save_client(client_id, client_secret, redirect_uris, client_name,
                grant_types, response_types, token_endpoint_auth_method):
    with db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO oauth_clients
               (client_id, client_secret, redirect_uris, client_name,
                grant_types, response_types, token_endpoint_auth_method)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (client_id, client_secret, json.dumps(redirect_uris),
             client_name, json.dumps(grant_types), json.dumps(response_types),
             token_endpoint_auth_method),
        )


def get_client(client_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM oauth_clients WHERE client_id = ?",
            (client_id,),
        ).fetchone()
    if row:
        d = dict(row)
        d["redirect_uris"] = json.loads(d["redirect_uris"])
        d["grant_types"] = json.loads(d["grant_types"])
        d["response_types"] = json.loads(d["response_types"])
        return d
    return None


# --- Auth Sessions ---

def create_auth_session(google_state, mcp_state, code_challenge,
                        mcp_client_id, redirect_uri, redirect_uri_provided,
                        service, scopes, resource=None):
    with db() as conn:
        conn.execute(
            """INSERT INTO auth_sessions
               (google_state, mcp_state, code_challenge, mcp_client_id,
                redirect_uri, redirect_uri_provided, service, scopes, resource)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (google_state, mcp_state, code_challenge, mcp_client_id,
             redirect_uri, redirect_uri_provided, service,
             json.dumps(scopes) if scopes else "[]", resource),
        )


def get_auth_session_by_google_state(google_state):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM auth_sessions WHERE google_state = ?",
            (google_state,),
        ).fetchone()
    return dict(row) if row else None


def get_auth_session_by_code(mcp_auth_code):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM auth_sessions WHERE mcp_auth_code = ?",
            (mcp_auth_code,),
        ).fetchone()
    return dict(row) if row else None


def complete_auth_session(google_state, mcp_auth_code, google_refresh_token,
                          google_access_token, user_email):
    with db() as conn:
        conn.execute(
            """UPDATE auth_sessions
               SET mcp_auth_code = ?, google_refresh_token = ?,
                   google_access_token = ?, user_email = ?,
                   created_at = unixepoch()
               WHERE google_state = ?""",
            (mcp_auth_code, google_refresh_token, google_access_token,
             user_email, google_state),
        )


def mark_code_used(mcp_auth_code):
    with db() as conn:
        conn.execute(
            "UPDATE auth_sessions SET code_used = 1 WHERE mcp_auth_code = ?",
            (mcp_auth_code,),
        )


# --- MCP Tokens ---

def save_mcp_token(token, token_type, mcp_client_id, user_email,
                   google_refresh_token, scopes, service, expires_at):
    with db() as conn:
        conn.execute(
            """INSERT INTO mcp_tokens
               (token, token_type, mcp_client_id, user_email,
                google_refresh_token, scopes, service, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (token, token_type, mcp_client_id, user_email,
             google_refresh_token, json.dumps(scopes), service, expires_at),
        )


def get_mcp_token(token):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM mcp_tokens WHERE token = ? AND revoked = 0",
            (token,),
        ).fetchone()
    if row:
        d = dict(row)
        d["scopes"] = json.loads(d["scopes"])
        return d
    return None


def revoke_tokens_for(mcp_client_id=None, token=None):
    with db() as conn:
        if token:
            conn.execute(
                "UPDATE mcp_tokens SET revoked = 1 WHERE token = ?",
                (token,),
            )
            # Also revoke paired token
            row = conn.execute(
                "SELECT user_email, mcp_client_id FROM mcp_tokens WHERE token = ?",
                (token,),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE mcp_tokens SET revoked = 1
                       WHERE user_email = ? AND mcp_client_id = ?""",
                    (row["user_email"], row["mcp_client_id"]),
                )
        elif mcp_client_id:
            conn.execute(
                "UPDATE mcp_tokens SET revoked = 1 WHERE mcp_client_id = ?",
                (mcp_client_id,),
            )


def get_google_creds_by_mcp_token(mcp_access_token: str) -> dict | None:
    """Look up Google refresh token for an MCP access token."""
    row = get_mcp_token(mcp_access_token)
    if row and row.get("google_refresh_token"):
        return {
            "refresh_token": row["google_refresh_token"],
            "user_email": row.get("user_email"),
            "service": row.get("service"),
        }
    return None
