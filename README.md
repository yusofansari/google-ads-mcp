# Google Ads MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that connects **Google Ads** to [Claude](https://claude.ai) and any MCP-compatible AI client — **57 tools** for campaign management, keyword optimization, bidding, and competitive analysis through natural language.

Ask *"which search terms are wasting budget?"* or *"compare campaign performance month over month"* and get answers — and actions — straight from your Google Ads account.

## Features

- **57 tools** over the `adwords` scope
- Campaign, ad group, and ad performance reporting
- Keyword optimization, quality scores, and match-type analysis
- Bid strategies and budget utilization
- Search-terms mining and wasteful-spend detection
- Auction insights and competitive analysis
- Raw **GAQL** query support
- Per-user OAuth 2.0 — every user connects their own Google Ads account

## Quick Start

### Use as a Claude Connector (no install)

1. Open [claude.ai/settings/connectors](https://claude.ai/settings/connectors)
2. Click **Add custom connector**
3. Enter: `https://saveyourclicks.com/mcp/gads`
4. Claude handles Google sign-in automatically via OAuth 2.0.

### Self-host

```bash
git clone https://github.com/YOUR_USERNAME/google-ads-mcp.git
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

*Keywords: Google Ads MCP, Google Ads API, Claude MCP, Model Context Protocol, PPC automation, GAQL, AI Google Ads management.*
