# Google Ads MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that connects **Google Ads** to any MCP-compatible AI client — [Claude](https://claude.ai), [Cursor](https://cursor.com), [Windsurf](https://windsurf.com), [Cline](https://cline.bot), [VS Code](https://code.visualstudio.com), ChatGPT, and more. **57 tools** for campaign management, keyword optimization, bidding, and competitive analysis through natural language.

Ask *"which search terms are wasting budget?"* or *"compare campaign performance month over month"* and get answers — and actions — straight from your Google Ads account.

## Features

- **57 tools** over the `adwords` scope
- Works with **any MCP client** (Claude, Cursor, Windsurf, Cline, VS Code, ChatGPT, …)
- Campaign, ad group, and ad performance reporting
- Keyword optimization, quality scores, and match-type analysis
- Bid strategies and budget utilization
- Search-terms mining and wasteful-spend detection
- Auction insights and competitive analysis
- Raw **GAQL** query support
- Per-user OAuth 2.0 — every user connects their own Google Ads account

## Quick Start

### Option A — Hosted connector (no install)

Add this remote MCP URL as a custom connector in any client that supports remote MCP + OAuth:

```
https://saveyourclicks.com/mcp/gads
```

- **Claude** — [claude.ai/settings/connectors](https://claude.ai/settings/connectors) → *Add custom connector*
- **Cursor / Windsurf / Cline / VS Code** — add it as an MCP server URL in your client's MCP settings

The server handles Google sign-in automatically via OAuth 2.0.

### Option B — Self-host

```bash
git clone https://github.com/yusofansari/google-ads-mcp.git
cd google-ads-mcp
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in your OAuth credentials + Google Ads developer token
python google_ads/run.py
```

You'll need a Google Cloud OAuth client and a **Google Ads API developer token** (from the Google Ads API Center). Production systemd + nginx examples are in [`deploy/`](deploy/).

## How it works

The server uses [FastMCP](https://github.com/modelcontextprotocol) with a custom OAuth 2.0 provider so each user authenticates with their own Google Ads account. Only OAuth tokens are persisted (short-lived access + refresh); Ads API responses stream straight through to the model. OAuth, token storage, and the ASGI wrapper live in [`shared/`](shared/).

## Privacy & Security

- **No data storage** — Google API data is never retained
- **Token-only storage** — only OAuth tokens are kept (24h access, 90d refresh)
- **Per-user isolation** — complete separation between users
- **HTTPS only** and compliant with the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy)

## License

[MIT](LICENSE)

## Author

**Yusof Ansari-Renani** — [saveyourclicks.com](https://saveyourclicks.com)

- [LinkedIn](https://www.linkedin.com/in/yusof-ansari-renani-325319222/)
- [Telegram](https://t.me/yusofansari)

---

*Keywords: Google Ads MCP, Google Ads API, Claude MCP, Cursor MCP, Model Context Protocol, PPC automation, GAQL, AI Google Ads management.*
