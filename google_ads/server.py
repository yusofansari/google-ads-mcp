"""
Google Ads MCP Server — multi-tenant public version.
Uses per-user OAuth credentials via MCP OAuth 2.0 flow.
"""

import sys, os  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from mcp.server.fastmcp import FastMCP

from shared.config import GADS_DEVELOPER_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
from shared.user_creds import get_current_google_refresh_token

# ─── Configuration ───────────────────────────────────────────────────────────

DEVELOPER_TOKEN = GADS_DEVELOPER_TOKEN

mcp = FastMCP("Google Ads")

# ─── Client helpers ──────────────────────────────────────────────────────────

from google.ads.googleads.client import GoogleAdsClient  # noqa: E402
from google.ads.googleads.errors import GoogleAdsException  # noqa: E402


def _client(login_customer_id: str = "") -> GoogleAdsClient:
    refresh_token = get_current_google_refresh_token()
    cfg = {
        "developer_token": DEVELOPER_TOKEN,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "use_proto_plus": True,
    }
    if login_customer_id:
        cfg["login_customer_id"] = login_customer_id.replace("-", "")
    return GoogleAdsClient.load_from_dict(cfg)


def _service():
    return _client().get_service("GoogleAdsService")


# ─── Utility helpers ─────────────────────────────────────────────────────────


def _resolve_date(d: str) -> str:
    """Convert relative date string to YYYY-MM-DD."""
    today = datetime.now()
    if d == "today":
        return today.strftime("%Y-%m-%d")
    if d == "yesterday":
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")
    if d.endswith("daysAgo"):
        n = int(d.replace("daysAgo", ""))
        return (today - timedelta(days=n)).strftime("%Y-%m-%d")
    return d  # assume YYYY-MM-DD


def _dates(start: str, end: str) -> tuple:
    return _resolve_date(start), _resolve_date(end)


def _micros(val) -> float:
    """Convert micros to currency units (e.g. USD)."""
    return round(val / 1_000_000, 2) if val else 0.0


def _value(val) -> float:
    """Round standard Google Ads value metrics.

    Metrics such as conversions_value are already returned as account currency
    values by the Google Ads API, unlike cost_micros.
    """
    return round(float(val), 2) if val else 0.0


def _pct(val) -> float:
    """Format ratio as percentage."""
    return round(val * 100, 2) if val else 0.0


def _safe_enum(val) -> str:
    """Safely get the name of a proto enum value."""
    try:
        return val.name
    except AttributeError:
        return str(val) if val else ""


def _cpa(cost_micros, conversions) -> float:
    if conversions and conversions > 0:
        return round((cost_micros / 1_000_000) / conversions, 2)
    return 0.0


def _query(gaql: str, customer_id: str = ""):
    """Execute GAQL query and return raw response iterator."""
    if not customer_id:
        raise ValueError(
            "customer_id is required. Use get_accessible_customers to find your account IDs."
        )
    cid = customer_id.replace("-", "")
    return _service().search(customer_id=cid, query=gaql)


def _stream(gaql: str, customer_id: str = ""):
    """Execute GAQL query with streaming for large result sets."""
    if not customer_id:
        raise ValueError(
            "customer_id is required. Use get_accessible_customers to find your account IDs."
        )
    cid = customer_id.replace("-", "")
    stream = _service().search_stream(customer_id=cid, query=gaql)
    for batch in stream:
        yield from batch.results


# ═════════════════════════════════════════════════════════════════════════════
#  1. GENERIC / CUSTOM TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def run_gaql_query(
    query: str,
    customer_id: str = "",
    limit: int = 500,
) -> dict:
    """
    Execute any GAQL (Google Ads Query Language) query.
    Use this for advanced queries not covered by other tools.
    Returns raw field values; costs are in micros (divide by 1,000,000 for USD).

    Example:
      query: "SELECT campaign.name, metrics.clicks FROM campaign WHERE segments.date DURING LAST_7_DAYS ORDER BY metrics.clicks DESC LIMIT 10"
    """
    if "LIMIT" not in query.upper():
        query = query.rstrip().rstrip(";") + f" LIMIT {limit}"

    rows = []
    for row in _query(query, customer_id or None):
        # Convert protobuf row to dict via JSON serialization
        from google.protobuf.json_format import MessageToDict
        pb = type(row).pb(row)
        d = MessageToDict(pb, preserving_proto_field_name=True)
        rows.append(d)

    return {"rows": rows, "row_count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  2. ACCOUNT TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_account_info(customer_id: str = "") -> dict:
    """
    Get account-level information: name, currency, timezone, auto-tagging, etc.
    """
    q = """
        SELECT
            customer.id,
            customer.descriptive_name,
            customer.currency_code,
            customer.time_zone,
            customer.auto_tagging_enabled,
            customer.tracking_url_template,
            customer.manager
        FROM customer
        LIMIT 1
    """
    for row in _query(q, customer_id or None):
        return {
            "customer_id": str(row.customer.id),
            "name": row.customer.descriptive_name,
            "currency": row.customer.currency_code,
            "timezone": row.customer.time_zone,
            "auto_tagging": row.customer.auto_tagging_enabled,
            "tracking_template": row.customer.tracking_url_template or "",
            "is_manager": row.customer.manager,
        }
    return {"error": "No account data returned"}


@mcp.tool()
def get_account_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    customer_id: str = "",
) -> dict:
    """
    Get daily account-level performance metrics.
    All cost values returned in USD (converted from micros).
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion,
            metrics.all_conversions,
            metrics.interactions,
            metrics.interaction_rate,
            metrics.search_impression_share
        FROM customer
        WHERE segments.date BETWEEN '{s}' AND '{e}'
        ORDER BY segments.date DESC
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "date": row.segments.date,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "conversion_value": _value(row.metrics.conversions_value),
            "ctr": _pct(row.metrics.ctr),
            "avg_cpc": _micros(row.metrics.average_cpc),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
            "all_conversions": round(row.metrics.all_conversions, 2),
            "interactions": row.metrics.interactions,
            "interaction_rate": _pct(row.metrics.interaction_rate),
            "search_impression_share": _pct(row.metrics.search_impression_share),
        })
    return {"rows": rows, "row_count": len(rows)}


@mcp.tool()
def get_accessible_customers() -> dict:
    """
    List all Google Ads customer IDs accessible with the current credentials.
    Useful for MCC (Manager) accounts to discover child accounts.
    """
    client = _client()
    customer_service = client.get_service("CustomerService")
    response = customer_service.list_accessible_customers()
    ids = [rn.split("/")[-1] for rn in response.resource_names]
    return {"customer_ids": ids, "count": len(ids)}


# ═════════════════════════════════════════════════════════════════════════════
#  3. CAMPAIGN TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def list_campaigns(
    status_filter: str = "all",
    customer_id: str = "",
) -> dict:
    """
    List all campaigns with basic info (name, status, type, budget).
    status_filter: "all", "enabled", "paused"
    """
    where = "campaign.status != 'REMOVED'"
    if status_filter == "enabled":
        where = "campaign.status = 'ENABLED'"
    elif status_filter == "paused":
        where = "campaign.status = 'PAUSED'"

    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            campaign.bidding_strategy_type,
            campaign_budget.amount_micros
        FROM campaign
        WHERE {where}
        ORDER BY campaign.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_id": str(row.campaign.id),
            "name": row.campaign.name,
            "status": _safe_enum(row.campaign.status),
            "channel_type": _safe_enum(row.campaign.advertising_channel_type),
            "bidding_strategy": _safe_enum(row.campaign.bidding_strategy_type),
            "daily_budget": _micros(row.campaign_budget.amount_micros),
        })
    return {"campaigns": rows, "count": len(rows)}


@mcp.tool()
def get_campaign_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    status_filter: str = "enabled",
    limit: int = 1000,
    customer_id: str = "",
) -> dict:
    """
    Get daily performance metrics by campaign.
    Includes impression share metrics for search campaigns.
    campaign_id: filter to specific campaign (optional).
    status_filter: "all", "enabled", "paused"
    """
    s, e = _dates(start_date, end_date)
    where = [f"segments.date BETWEEN '{s}' AND '{e}'"]
    if status_filter == "enabled":
        where.append("campaign.status = 'ENABLED'")
    elif status_filter == "paused":
        where.append("campaign.status = 'PAUSED'")
    else:
        where.append("campaign.status != 'REMOVED'")
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            campaign.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion,
            metrics.conversions_from_interactions_rate,
            metrics.search_impression_share,
            metrics.search_budget_lost_impression_share,
            metrics.search_rank_lost_impression_share,
            campaign_budget.amount_micros
        FROM campaign
        WHERE {' AND '.join(where)}
        ORDER BY segments.date DESC, metrics.cost_micros DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        budget_micros = row.campaign_budget.amount_micros
        cost_micros = row.metrics.cost_micros
        rows.append({
            "date": row.segments.date,
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "status": _safe_enum(row.campaign.status),
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "conversion_value": _value(row.metrics.conversions_value),
            "ctr": _pct(row.metrics.ctr),
            "avg_cpc": _micros(row.metrics.average_cpc),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
            "conversion_rate": _pct(row.metrics.conversions_from_interactions_rate),
            "search_impression_share": _pct(row.metrics.search_impression_share),
            "search_budget_lost_is": _pct(row.metrics.search_budget_lost_impression_share),
            "search_rank_lost_is": _pct(row.metrics.search_rank_lost_impression_share),
            "daily_budget": _micros(budget_micros),
            "budget_utilization": round(cost_micros / budget_micros, 4) if budget_micros > 0 else 0,
        })
    return {"rows": rows, "row_count": len(rows)}


@mcp.tool()
def get_campaign_settings(
    status_filter: str = "enabled",
    customer_id: str = "",
) -> dict:
    """
    Get campaign settings: bid strategy, target CPA/ROAS, network targeting, budget.
    """
    where = "campaign.status = 'ENABLED'" if status_filter == "enabled" else "campaign.status != 'REMOVED'"
    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.bidding_strategy_type,
            campaign.target_cpa.target_cpa_micros,
            campaign.maximize_conversions.target_cpa_micros,
            campaign.target_roas.target_roas,
            campaign.maximize_conversion_value.target_roas,
            campaign.network_settings.target_google_search,
            campaign.network_settings.target_search_network,
            campaign.network_settings.target_content_network,
            campaign_budget.amount_micros,
            campaign_budget.explicitly_shared
        FROM campaign
        WHERE {where}
        ORDER BY campaign.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        target_cpa = (
            row.campaign.target_cpa.target_cpa_micros
            or row.campaign.maximize_conversions.target_cpa_micros
            or 0
        )
        target_roas = (
            row.campaign.target_roas.target_roas
            or row.campaign.maximize_conversion_value.target_roas
            or 0
        )
        rows.append({
            "campaign_id": str(row.campaign.id),
            "name": row.campaign.name,
            "status": _safe_enum(row.campaign.status),
            "bid_strategy": _safe_enum(row.campaign.bidding_strategy_type),
            "target_cpa": _micros(target_cpa),
            "target_roas": round(target_roas, 2) if target_roas else 0,
            "daily_budget": _micros(row.campaign_budget.amount_micros),
            "shared_budget": row.campaign_budget.explicitly_shared,
            "google_search": row.campaign.network_settings.target_google_search,
            "search_partners": row.campaign.network_settings.target_search_network,
            "display_network": row.campaign.network_settings.target_content_network,
        })
    return {"campaigns": rows, "count": len(rows)}


@mcp.tool()
def get_campaign_by_type(
    channel_type: str = "",
    customer_id: str = "",
) -> dict:
    """
    List campaigns filtered by advertising channel type.
    channel_type: SEARCH, DISPLAY, SHOPPING, VIDEO, PERFORMANCE_MAX, DEMAND_GEN, etc.
    Leave empty to see all with their types.
    """
    where = "campaign.status != 'REMOVED'"
    if channel_type:
        where += f" AND campaign.advertising_channel_type = '{channel_type}'"

    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            campaign.advertising_channel_type,
            campaign.advertising_channel_sub_type,
            campaign_budget.amount_micros
        FROM campaign
        WHERE {where}
        ORDER BY campaign.advertising_channel_type, campaign.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_id": str(row.campaign.id),
            "name": row.campaign.name,
            "status": _safe_enum(row.campaign.status),
            "channel_type": _safe_enum(row.campaign.advertising_channel_type),
            "channel_sub_type": _safe_enum(row.campaign.advertising_channel_sub_type),
            "daily_budget": _micros(row.campaign_budget.amount_micros),
        })
    return {"campaigns": rows, "count": len(rows)}


@mcp.tool()
def get_campaign_budget_utilization(
    start_date: str = "7daysAgo",
    end_date: str = "yesterday",
    customer_id: str = "",
) -> dict:
    """
    Analyze budget utilization: how much of daily budget each campaign is spending.
    Helps identify under-spending (opportunity) and budget-capped campaigns.
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            metrics.cost_micros,
            campaign_budget.amount_micros,
            metrics.impressions,
            metrics.clicks,
            metrics.conversions,
            metrics.search_budget_lost_impression_share
        FROM campaign
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND campaign.status = 'ENABLED'
        ORDER BY metrics.cost_micros DESC
    """
    # Aggregate by campaign
    agg = {}
    for row in _query(q, customer_id or None):
        cid = str(row.campaign.id)
        if cid not in agg:
            agg[cid] = {
                "campaign_id": cid,
                "campaign_name": row.campaign.name,
                "total_cost": 0,
                "daily_budget": _micros(row.campaign_budget.amount_micros),
                "impressions": 0,
                "clicks": 0,
                "conversions": 0.0,
                "budget_lost_is_samples": [],
            }
        agg[cid]["total_cost"] += row.metrics.cost_micros
        agg[cid]["impressions"] += row.metrics.impressions
        agg[cid]["clicks"] += row.metrics.clicks
        agg[cid]["conversions"] += row.metrics.conversions
        if row.metrics.search_budget_lost_impression_share:
            agg[cid]["budget_lost_is_samples"].append(
                row.metrics.search_budget_lost_impression_share
            )

    # Calculate days in range
    d1 = datetime.strptime(s, "%Y-%m-%d")
    d2 = datetime.strptime(e, "%Y-%m-%d")
    num_days = max((d2 - d1).days + 1, 1)

    rows = []
    for c in agg.values():
        avg_daily_spend = _micros(c["total_cost"]) / num_days
        util = round(avg_daily_spend / c["daily_budget"], 4) if c["daily_budget"] > 0 else 0
        samples = c["budget_lost_is_samples"]
        avg_budget_lost = round(sum(samples) / len(samples) * 100, 2) if samples else 0
        rows.append({
            "campaign_id": c["campaign_id"],
            "campaign_name": c["campaign_name"],
            "daily_budget": c["daily_budget"],
            "avg_daily_spend": round(avg_daily_spend, 2),
            "total_cost": _micros(c["total_cost"]),
            "budget_utilization": round(util * 100, 2),
            "impressions": c["impressions"],
            "clicks": c["clicks"],
            "conversions": round(c["conversions"], 2),
            "avg_budget_lost_impression_share": avg_budget_lost,
            "days_in_range": num_days,
        })
    rows.sort(key=lambda x: x["budget_utilization"], reverse=True)
    return {"rows": rows, "row_count": len(rows)}


@mcp.tool()
def get_campaign_performance_comparison(
    period1_start: str = "60daysAgo",
    period1_end: str = "31daysAgo",
    period2_start: str = "30daysAgo",
    period2_end: str = "yesterday",
    customer_id: str = "",
) -> dict:
    """
    Compare campaign performance between two date ranges.
    Default: previous 30 days vs. last 30 days.
    Returns metrics for both periods with percentage change.
    """
    def _fetch_period(start, end):
        s, e = _dates(start, end)
        q = f"""
            SELECT
                campaign.id,
                campaign.name,
                metrics.impressions,
                metrics.clicks,
                metrics.cost_micros,
                metrics.conversions,
                metrics.conversions_value,
                metrics.ctr,
                metrics.average_cpc,
                metrics.cost_per_conversion
            FROM campaign
            WHERE segments.date BETWEEN '{s}' AND '{e}'
                AND campaign.status != 'REMOVED'
        """
        agg = {}
        for row in _query(q, customer_id or None):
            cid = str(row.campaign.id)
            if cid not in agg:
                agg[cid] = {
                    "campaign_id": cid,
                    "name": row.campaign.name,
                    "impressions": 0, "clicks": 0,
                    "cost_micros": 0, "conversions": 0.0,
                    "conversion_value": 0,
                }
            agg[cid]["impressions"] += row.metrics.impressions
            agg[cid]["clicks"] += row.metrics.clicks
            agg[cid]["cost_micros"] += row.metrics.cost_micros
            agg[cid]["conversions"] += row.metrics.conversions
            agg[cid]["conversion_value"] += row.metrics.conversions_value
        return agg

    p1 = _fetch_period(period1_start, period1_end)
    p2 = _fetch_period(period2_start, period2_end)

    all_ids = set(p1.keys()) | set(p2.keys())
    results = []
    for cid in all_ids:
        d1 = p1.get(cid, {"impressions": 0, "clicks": 0, "cost_micros": 0, "conversions": 0, "conversion_value": 0, "name": ""})
        d2 = p2.get(cid, {"impressions": 0, "clicks": 0, "cost_micros": 0, "conversions": 0, "conversion_value": 0, "name": ""})
        name = d2.get("name") or d1.get("name") or ""

        def chg(v1, v2):
            if v1 == 0:
                return 100.0 if v2 > 0 else 0.0
            return round((v2 - v1) / v1 * 100, 2)

        results.append({
            "campaign_id": cid,
            "campaign_name": name,
            "period1_impressions": d1["impressions"],
            "period2_impressions": d2["impressions"],
            "impressions_change": chg(d1["impressions"], d2["impressions"]),
            "period1_clicks": d1["clicks"],
            "period2_clicks": d2["clicks"],
            "clicks_change": chg(d1["clicks"], d2["clicks"]),
            "period1_cost": _micros(d1["cost_micros"]),
            "period2_cost": _micros(d2["cost_micros"]),
            "cost_change": chg(d1["cost_micros"], d2["cost_micros"]),
            "period1_conversions": round(d1["conversions"], 2),
            "period2_conversions": round(d2["conversions"], 2),
            "conversions_change": chg(d1["conversions"], d2["conversions"]),
        })
    results.sort(key=lambda x: abs(x["cost_change"]), reverse=True)
    return {"comparisons": results, "count": len(results)}


# ═════════════════════════════════════════════════════════════════════════════
#  4. AD GROUP TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def list_ad_groups(
    campaign_id: str = "",
    status_filter: str = "all",
    customer_id: str = "",
) -> dict:
    """
    List ad groups with basic info.
    campaign_id: filter to a specific campaign (optional).
    status_filter: "all", "enabled", "paused"
    """
    where = ["ad_group.status != 'REMOVED'"]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")
    if status_filter == "enabled":
        where.append("ad_group.status = 'ENABLED'")
    elif status_filter == "paused":
        where.append("ad_group.status = 'PAUSED'")

    q = f"""
        SELECT
            ad_group.id,
            ad_group.name,
            ad_group.status,
            ad_group.type,
            ad_group.cpc_bid_micros,
            campaign.id,
            campaign.name
        FROM ad_group
        WHERE {' AND '.join(where)}
        ORDER BY campaign.name, ad_group.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "ad_group_id": str(row.ad_group.id),
            "name": row.ad_group.name,
            "status": _safe_enum(row.ad_group.status),
            "type": _safe_enum(row.ad_group.type),
            "cpc_bid": _micros(row.ad_group.cpc_bid_micros),
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
        })
    return {"ad_groups": rows, "count": len(rows)}


@mcp.tool()
def get_ad_group_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    ad_group_id: str = "",
    limit: int = 500,
    customer_id: str = "",
) -> dict:
    """
    Get daily ad group performance metrics.
    Filter by campaign_id and/or ad_group_id (optional).
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "ad_group.status != 'REMOVED'",
        "campaign.status != 'REMOVED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")
    if ad_group_id:
        where.append(f"ad_group.id = {ad_group_id}")

    q = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion
        FROM ad_group
        WHERE {' AND '.join(where)}
        ORDER BY segments.date DESC, metrics.cost_micros DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "date": row.segments.date,
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "ad_group_id": str(row.ad_group.id),
            "ad_group_name": row.ad_group.name,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "conversion_value": _value(row.metrics.conversions_value),
            "ctr": _pct(row.metrics.ctr),
            "avg_cpc": _micros(row.metrics.average_cpc),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"rows": rows, "row_count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  5. AD TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_ad_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    ad_group_id: str = "",
    limit: int = 500,
    customer_id: str = "",
) -> dict:
    """
    Get ad-level performance metrics.
    Includes ad type, final URLs, and ad strength.
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "ad_group_ad.status != 'REMOVED'",
        "campaign.status != 'REMOVED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")
    if ad_group_id:
        where.append(f"ad_group.id = {ad_group_id}")

    q = f"""
        SELECT
            campaign.name,
            ad_group.name,
            ad_group_ad.ad.id,
            ad_group_ad.ad.type,
            ad_group_ad.ad.final_urls,
            ad_group_ad.status,
            ad_group_ad.ad_strength,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion
        FROM ad_group_ad
        WHERE {' AND '.join(where)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        urls = list(row.ad_group_ad.ad.final_urls) if row.ad_group_ad.ad.final_urls else []
        rows.append({
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "ad_id": str(row.ad_group_ad.ad.id),
            "ad_type": _safe_enum(row.ad_group_ad.ad.type),
            "final_urls": urls,
            "status": _safe_enum(row.ad_group_ad.status),
            "ad_strength": _safe_enum(row.ad_group_ad.ad_strength),
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "ctr": _pct(row.metrics.ctr),
            "avg_cpc": _micros(row.metrics.average_cpc),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"rows": rows, "row_count": len(rows)}


@mcp.tool()
def get_responsive_search_ad_assets(
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get Responsive Search Ad asset (headline/description) performance.
    Shows which headlines and descriptions are performing best.
    """
    where = [
        "ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'",
        "campaign.status = 'ENABLED'",
        "ad_group_ad.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.name,
            ad_group.name,
            ad_group_ad_asset_view.field_type,
            ad_group_ad_asset_view.performance_label,
            asset.text_asset.text,
            metrics.impressions,
            metrics.clicks,
            metrics.ctr
        FROM ad_group_ad_asset_view
        WHERE {' AND '.join(where)}
        ORDER BY metrics.impressions DESC
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "field_type": _safe_enum(row.ad_group_ad_asset_view.field_type),
            "performance_label": _safe_enum(row.ad_group_ad_asset_view.performance_label),
            "text": row.asset.text_asset.text or "",
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "ctr": _pct(row.metrics.ctr),
        })
    return {"assets": rows, "count": len(rows)}


@mcp.tool()
def get_ad_strength_overview(
    customer_id: str = "",
) -> dict:
    """
    Overview of ad strength across all active RSAs.
    Shows distribution of EXCELLENT, GOOD, AVERAGE, POOR ratings.
    """
    q = """
        SELECT
            campaign.name,
            ad_group.name,
            ad_group_ad.ad.id,
            ad_group_ad.ad_strength,
            ad_group_ad.policy_summary.approval_status
        FROM ad_group_ad
        WHERE ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'
            AND ad_group_ad.status = 'ENABLED'
            AND campaign.status = 'ENABLED'
        ORDER BY campaign.name, ad_group.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "ad_id": str(row.ad_group_ad.ad.id),
            "ad_strength": _safe_enum(row.ad_group_ad.ad_strength),
            "approval_status": _safe_enum(row.ad_group_ad.policy_summary.approval_status),
        })
    # Summary
    strength_dist = {}
    for r in rows:
        s = r["ad_strength"]
        strength_dist[s] = strength_dist.get(s, 0) + 1
    return {"ads": rows, "count": len(rows), "strength_distribution": strength_dist}


# ═════════════════════════════════════════════════════════════════════════════
#  6. KEYWORD TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_keyword_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    ad_group_id: str = "",
    limit: int = 500,
    customer_id: str = "",
) -> dict:
    """
    Get keyword-level performance metrics including quality score.
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status != 'REMOVED'",
        "ad_group.status != 'REMOVED'",
        "ad_group_criterion.status != 'REMOVED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")
    if ad_group_id:
        where.append(f"ad_group.id = {ad_group_id}")

    q = f"""
        SELECT
            segments.date,
            campaign.id,
            campaign.name,
            ad_group.id,
            ad_group.name,
            ad_group_criterion.criterion_id,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.quality_info.quality_score,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion
        FROM keyword_view
        WHERE {' AND '.join(where)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "date": row.segments.date,
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "ad_group_id": str(row.ad_group.id),
            "ad_group_name": row.ad_group.name,
            "keyword_id": str(row.ad_group_criterion.criterion_id),
            "keyword": row.ad_group_criterion.keyword.text,
            "match_type": _safe_enum(row.ad_group_criterion.keyword.match_type),
            "quality_score": row.ad_group_criterion.quality_info.quality_score or 0,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "ctr": _pct(row.metrics.ctr),
            "avg_cpc": _micros(row.metrics.average_cpc),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"rows": rows, "row_count": len(rows)}


@mcp.tool()
def get_keyword_quality_scores(
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get detailed quality score breakdown for all active keywords.
    Includes overall QS, expected CTR, ad relevance, and landing page experience.
    """
    where = [
        "campaign.status = 'ENABLED'",
        "ad_group.status = 'ENABLED'",
        "ad_group_criterion.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.name,
            ad_group.name,
            ad_group_criterion.criterion_id,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.quality_info.quality_score,
            ad_group_criterion.quality_info.creative_quality_score,
            ad_group_criterion.quality_info.search_predicted_ctr,
            ad_group_criterion.quality_info.post_click_quality_score
        FROM keyword_view
        WHERE {' AND '.join(where)}
        ORDER BY ad_group_criterion.quality_info.quality_score ASC
    """
    rows = []
    for row in _query(q, customer_id or None):
        qi = row.ad_group_criterion.quality_info
        rows.append({
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "keyword_id": str(row.ad_group_criterion.criterion_id),
            "keyword": row.ad_group_criterion.keyword.text,
            "match_type": _safe_enum(row.ad_group_criterion.keyword.match_type),
            "quality_score": qi.quality_score or 0,
            "expected_ctr": _safe_enum(qi.search_predicted_ctr),
            "ad_relevance": _safe_enum(qi.creative_quality_score),
            "landing_page_exp": _safe_enum(qi.post_click_quality_score),
        })
    return {"keywords": rows, "count": len(rows)}


@mcp.tool()
def get_keyword_match_type_analysis(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    customer_id: str = "",
) -> dict:
    """
    Analyze performance by keyword match type (BROAD, PHRASE, EXACT).
    Aggregated across all campaigns.
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            ad_group_criterion.keyword.match_type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion
        FROM keyword_view
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
            AND ad_group_criterion.status = 'ENABLED'
    """
    agg = {}
    for row in _query(q, customer_id or None):
        mt = _safe_enum(row.ad_group_criterion.keyword.match_type)
        if mt not in agg:
            agg[mt] = {"impressions": 0, "clicks": 0, "cost_micros": 0,
                       "conversions": 0.0, "conversion_value": 0}
        agg[mt]["impressions"] += row.metrics.impressions
        agg[mt]["clicks"] += row.metrics.clicks
        agg[mt]["cost_micros"] += row.metrics.cost_micros
        agg[mt]["conversions"] += row.metrics.conversions
        agg[mt]["conversion_value"] += row.metrics.conversions_value

    rows = []
    for mt, d in agg.items():
        rows.append({
            "match_type": mt,
            "impressions": d["impressions"],
            "clicks": d["clicks"],
            "cost": _micros(d["cost_micros"]),
            "conversions": round(d["conversions"], 2),
            "conversion_value": _value(d["conversion_value"]),
            "ctr": _pct(d["clicks"] / d["impressions"]) if d["impressions"] > 0 else 0,
            "avg_cpc": _micros(d["cost_micros"] / d["clicks"]) if d["clicks"] > 0 else 0,
            "cost_per_conversion": _cpa(d["cost_micros"], d["conversions"]),
        })
    return {"match_types": rows, "count": len(rows)}


@mcp.tool()
def get_top_keywords_by_conversions(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    limit: int = 50,
    customer_id: str = "",
) -> dict:
    """
    Get top keywords ranked by conversions.
    Useful for identifying your best-performing keywords.
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            campaign.name,
            ad_group.name,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.quality_info.quality_score,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM keyword_view
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
            AND ad_group_criterion.status = 'ENABLED'
            AND metrics.conversions > 0
        ORDER BY metrics.conversions DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "keyword": row.ad_group_criterion.keyword.text,
            "match_type": _safe_enum(row.ad_group_criterion.keyword.match_type),
            "quality_score": row.ad_group_criterion.quality_info.quality_score or 0,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "conversion_value": _value(row.metrics.conversions_value),
            "ctr": _pct(row.metrics.ctr),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"keywords": rows, "count": len(rows)}


@mcp.tool()
def get_low_performing_keywords(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    min_cost: float = 10.0,
    max_conversions: float = 0.5,
    limit: int = 100,
    customer_id: str = "",
) -> dict:
    """
    Find keywords with high cost but low/zero conversions (wasteful spend).
    min_cost: minimum spend in USD to include.
    max_conversions: maximum conversions to flag as low-performing.
    """
    s, e = _dates(start_date, end_date)
    min_cost_micros = int(min_cost * 1_000_000)
    q = f"""
        SELECT
            campaign.name,
            ad_group.name,
            ad_group_criterion.keyword.text,
            ad_group_criterion.keyword.match_type,
            ad_group_criterion.quality_info.quality_score,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM keyword_view
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND campaign.status = 'ENABLED'
            AND ad_group.status = 'ENABLED'
            AND ad_group_criterion.status = 'ENABLED'
            AND metrics.cost_micros >= {min_cost_micros}
            AND metrics.conversions <= {max_conversions}
        ORDER BY metrics.cost_micros DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "keyword": row.ad_group_criterion.keyword.text,
            "match_type": _safe_enum(row.ad_group_criterion.keyword.match_type),
            "quality_score": row.ad_group_criterion.quality_info.quality_score or 0,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "ctr": _pct(row.metrics.ctr),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"keywords": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  7. SEARCH TERM TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_search_terms(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    limit: int = 500,
    customer_id: str = "",
) -> dict:
    """
    Get search terms report showing actual user queries that triggered ads.
    Sorted by cost (highest spend first).
    """
    s, e = _dates(start_date, end_date)
    where = [f"segments.date BETWEEN '{s}' AND '{e}'"]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            ad_group.name,
            search_term_view.search_term,
            search_term_view.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc
        FROM search_term_view
        WHERE {' AND '.join(where)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "search_term": row.search_term_view.search_term,
            "status": _safe_enum(row.search_term_view.status),
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "conversion_value": _value(row.metrics.conversions_value),
            "ctr": _pct(row.metrics.ctr),
            "avg_cpc": _micros(row.metrics.average_cpc),
        })
    return {"rows": rows, "row_count": len(rows)}


@mcp.tool()
def get_wasteful_search_terms(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    min_cost: float = 5.0,
    limit: int = 100,
    customer_id: str = "",
) -> dict:
    """
    Find search terms with spend but zero conversions (candidates for negative keywords).
    min_cost: minimum spend in USD.
    """
    s, e = _dates(start_date, end_date)
    min_micros = int(min_cost * 1_000_000)
    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            ad_group.name,
            search_term_view.search_term,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr
        FROM search_term_view
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND metrics.cost_micros >= {min_micros}
            AND metrics.conversions = 0
        ORDER BY metrics.cost_micros DESC
        LIMIT {limit}
    """
    rows = []
    total_waste = 0
    for row in _query(q, customer_id or None):
        cost = _micros(row.metrics.cost_micros)
        total_waste += cost
        rows.append({
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "search_term": row.search_term_view.search_term,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": cost,
            "ctr": _pct(row.metrics.ctr),
        })
    return {
        "wasteful_terms": rows,
        "count": len(rows),
        "total_wasted_spend": round(total_waste, 2),
    }


@mcp.tool()
def get_converting_search_terms(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    limit: int = 100,
    customer_id: str = "",
) -> dict:
    """
    Find search terms that have driven conversions.
    Useful for finding new keyword opportunities.
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            ad_group.name,
            search_term_view.search_term,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.cost_per_conversion,
            metrics.ctr
        FROM search_term_view
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND metrics.conversions > 0
        ORDER BY metrics.conversions DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "search_term": row.search_term_view.search_term,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "conversion_value": _value(row.metrics.conversions_value),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
            "ctr": _pct(row.metrics.ctr),
        })
    return {"converting_terms": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  8. NEGATIVE KEYWORD TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_negative_keywords(
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get all campaign-level negative keywords.
    campaign_id: filter to specific campaign (optional).
    """
    where = [
        "campaign_criterion.negative = TRUE",
        "campaign_criterion.type = 'KEYWORD'",
        "campaign.status != 'REMOVED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign_criterion.criterion_id,
            campaign_criterion.keyword.text,
            campaign_criterion.keyword.match_type,
            campaign_criterion.resource_name
        FROM campaign_criterion
        WHERE {' AND '.join(where)}
        ORDER BY campaign.name, campaign_criterion.keyword.text
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "criterion_id": str(row.campaign_criterion.criterion_id),
            "keyword": row.campaign_criterion.keyword.text,
            "match_type": _safe_enum(row.campaign_criterion.keyword.match_type),
            "resource_name": row.campaign_criterion.resource_name,
        })
    return {"negative_keywords": rows, "count": len(rows)}


@mcp.tool()
def get_shared_negative_lists(
    customer_id: str = "",
) -> dict:
    """
    Get shared negative keyword lists (account-level) and their keywords.
    """
    q = """
        SELECT
            shared_set.id,
            shared_set.name,
            shared_set.type,
            shared_set.member_count,
            shared_set.status
        FROM shared_set
        WHERE shared_set.type = 'NEGATIVE_KEYWORDS'
            AND shared_set.status = 'ENABLED'
    """
    cid = (customer_id or CUSTOMER_ID).replace("-", "")
    lists = []
    for row in _query(q, cid):
        set_id = str(row.shared_set.id)
        set_name = row.shared_set.name

        # Fetch keywords in this set
        kw_q = f"""
            SELECT
                shared_criterion.keyword.text,
                shared_criterion.keyword.match_type
            FROM shared_criterion
            WHERE shared_set.name = '{set_name}'
        """
        keywords = []
        try:
            for kr in _query(kw_q, cid):
                keywords.append({
                    "keyword": kr.shared_criterion.keyword.text,
                    "match_type": _safe_enum(kr.shared_criterion.keyword.match_type),
                })
        except Exception:
            pass

        lists.append({
            "set_id": set_id,
            "name": set_name,
            "member_count": row.shared_set.member_count,
            "keywords": keywords,
        })
    return {"shared_lists": lists, "count": len(lists)}


# ═════════════════════════════════════════════════════════════════════════════
#  9. GEOGRAPHIC TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_geographic_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    limit: int = 100,
    customer_id: str = "",
) -> dict:
    """
    Get performance metrics broken down by country.
    Uses the geographic_view which shows where ads were shown.
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.status,
            campaign.name,
            geographic_view.country_criterion_id,
            geographic_view.location_type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion
        FROM geographic_view
        WHERE {' AND '.join(where)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_name": row.campaign.name,
            "country_criterion_id": str(row.geographic_view.country_criterion_id),
            "location_type": _safe_enum(row.geographic_view.location_type),
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "conversion_value": _value(row.metrics.conversions_value),
            "ctr": _pct(row.metrics.ctr),
            "avg_cpc": _micros(row.metrics.average_cpc),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"rows": rows, "row_count": len(rows)}


@mcp.tool()
def get_user_location_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    limit: int = 100,
    customer_id: str = "",
) -> dict:
    """
    Get performance by user's physical location (not where ad was targeted).
    Shows actual user locations from Google's location detection.
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.status,
            campaign.name,
            user_location_view.country_criterion_id,
            user_location_view.targeting_location,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM user_location_view
        WHERE {' AND '.join(where)}
        ORDER BY metrics.cost_micros DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_name": row.campaign.name,
            "country_criterion_id": str(row.user_location_view.country_criterion_id),
            "is_targeting_location": row.user_location_view.targeting_location,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "ctr": _pct(row.metrics.ctr),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"rows": rows, "row_count": len(rows)}


@mcp.tool()
def get_location_targets(
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get location targeting settings for campaigns.
    Shows which geographic locations are targeted or excluded.
    """
    where = [
        "campaign.status = 'ENABLED'",
        "campaign_criterion.type = 'LOCATION'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign_criterion.location.geo_target_constant,
            campaign_criterion.negative
        FROM campaign_criterion
        WHERE {' AND '.join(where)}
        ORDER BY campaign.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "geo_target": row.campaign_criterion.location.geo_target_constant or "",
            "is_excluded": row.campaign_criterion.negative,
        })
    return {"location_targets": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  10. DEMOGRAPHICS & DEVICE TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_device_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get performance breakdown by device type (DESKTOP, MOBILE, TABLET).
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            segments.device,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion
        FROM campaign
        WHERE {' AND '.join(where)}
    """
    agg = {}
    for row in _query(q, customer_id or None):
        dev = _safe_enum(row.segments.device)
        if dev not in agg:
            agg[dev] = {"impressions": 0, "clicks": 0, "cost_micros": 0,
                        "conversions": 0.0, "conversion_value": 0}
        agg[dev]["impressions"] += row.metrics.impressions
        agg[dev]["clicks"] += row.metrics.clicks
        agg[dev]["cost_micros"] += row.metrics.cost_micros
        agg[dev]["conversions"] += row.metrics.conversions
        agg[dev]["conversion_value"] += row.metrics.conversions_value

    rows = []
    for dev, d in agg.items():
        rows.append({
            "device": dev,
            "impressions": d["impressions"],
            "clicks": d["clicks"],
            "cost": _micros(d["cost_micros"]),
            "conversions": round(d["conversions"], 2),
            "conversion_value": _value(d["conversion_value"]),
            "ctr": _pct(d["clicks"] / d["impressions"]) if d["impressions"] > 0 else 0,
            "avg_cpc": _micros(d["cost_micros"] / d["clicks"]) if d["clicks"] > 0 else 0,
            "cost_per_conversion": _cpa(d["cost_micros"], d["conversions"]),
        })
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return {"devices": rows, "count": len(rows)}


@mcp.tool()
def get_age_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get performance breakdown by age range.
    Ranges: 18-24, 25-34, 35-44, 45-54, 55-64, 65+, UNDETERMINED.
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            ad_group_criterion.age_range.type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM age_range_view
        WHERE {' AND '.join(where)}
    """
    agg = {}
    for row in _query(q, customer_id or None):
        age = _safe_enum(row.ad_group_criterion.age_range.type)
        if age not in agg:
            agg[age] = {"impressions": 0, "clicks": 0, "cost_micros": 0, "conversions": 0.0}
        agg[age]["impressions"] += row.metrics.impressions
        agg[age]["clicks"] += row.metrics.clicks
        agg[age]["cost_micros"] += row.metrics.cost_micros
        agg[age]["conversions"] += row.metrics.conversions

    rows = []
    for age, d in agg.items():
        rows.append({
            "age_range": age,
            "impressions": d["impressions"],
            "clicks": d["clicks"],
            "cost": _micros(d["cost_micros"]),
            "conversions": round(d["conversions"], 2),
            "ctr": _pct(d["clicks"] / d["impressions"]) if d["impressions"] > 0 else 0,
            "cost_per_conversion": _cpa(d["cost_micros"], d["conversions"]),
        })
    return {"age_ranges": rows, "count": len(rows)}


@mcp.tool()
def get_gender_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get performance breakdown by gender (MALE, FEMALE, UNDETERMINED).
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            ad_group_criterion.gender.type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM gender_view
        WHERE {' AND '.join(where)}
    """
    agg = {}
    for row in _query(q, customer_id or None):
        gender = _safe_enum(row.ad_group_criterion.gender.type)
        if gender not in agg:
            agg[gender] = {"impressions": 0, "clicks": 0, "cost_micros": 0, "conversions": 0.0}
        agg[gender]["impressions"] += row.metrics.impressions
        agg[gender]["clicks"] += row.metrics.clicks
        agg[gender]["cost_micros"] += row.metrics.cost_micros
        agg[gender]["conversions"] += row.metrics.conversions

    rows = []
    for g, d in agg.items():
        rows.append({
            "gender": g,
            "impressions": d["impressions"],
            "clicks": d["clicks"],
            "cost": _micros(d["cost_micros"]),
            "conversions": round(d["conversions"], 2),
            "ctr": _pct(d["clicks"] / d["impressions"]) if d["impressions"] > 0 else 0,
            "cost_per_conversion": _cpa(d["cost_micros"], d["conversions"]),
        })
    return {"genders": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  11. TIME ANALYSIS TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_hourly_performance(
    start_date: str = "7daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get performance by hour of day (0-23).
    Useful for identifying peak hours and ad scheduling optimization.
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            segments.hour,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM campaign
        WHERE {' AND '.join(where)}
    """
    agg = {}
    for row in _query(q, customer_id or None):
        h = row.segments.hour
        if h not in agg:
            agg[h] = {"impressions": 0, "clicks": 0, "cost_micros": 0, "conversions": 0.0}
        agg[h]["impressions"] += row.metrics.impressions
        agg[h]["clicks"] += row.metrics.clicks
        agg[h]["cost_micros"] += row.metrics.cost_micros
        agg[h]["conversions"] += row.metrics.conversions

    rows = []
    for h in sorted(agg.keys()):
        d = agg[h]
        rows.append({
            "hour": h,
            "impressions": d["impressions"],
            "clicks": d["clicks"],
            "cost": _micros(d["cost_micros"]),
            "conversions": round(d["conversions"], 2),
            "ctr": _pct(d["clicks"] / d["impressions"]) if d["impressions"] > 0 else 0,
            "cost_per_conversion": _cpa(d["cost_micros"], d["conversions"]),
        })
    return {"hours": rows, "count": len(rows)}


@mcp.tool()
def get_day_of_week_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get performance by day of week (MONDAY through SUNDAY).
    Helps identify which days deliver best results.
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            segments.day_of_week,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM campaign
        WHERE {' AND '.join(where)}
    """
    agg = {}
    for row in _query(q, customer_id or None):
        dow = _safe_enum(row.segments.day_of_week)
        if dow not in agg:
            agg[dow] = {"impressions": 0, "clicks": 0, "cost_micros": 0,
                        "conversions": 0.0, "conversion_value": 0}
        agg[dow]["impressions"] += row.metrics.impressions
        agg[dow]["clicks"] += row.metrics.clicks
        agg[dow]["cost_micros"] += row.metrics.cost_micros
        agg[dow]["conversions"] += row.metrics.conversions
        agg[dow]["conversion_value"] += row.metrics.conversions_value

    day_order = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    rows = []
    for dow in day_order:
        if dow in agg:
            d = agg[dow]
            rows.append({
                "day_of_week": dow,
                "impressions": d["impressions"],
                "clicks": d["clicks"],
                "cost": _micros(d["cost_micros"]),
                "conversions": round(d["conversions"], 2),
                "conversion_value": _value(d["conversion_value"]),
                "ctr": _pct(d["clicks"] / d["impressions"]) if d["impressions"] > 0 else 0,
                "cost_per_conversion": _cpa(d["cost_micros"], d["conversions"]),
            })
    return {"days": rows, "count": len(rows)}


@mcp.tool()
def get_monthly_trends(
    months: int = 6,
    customer_id: str = "",
) -> dict:
    """
    Get monthly aggregated performance trends.
    months: number of months to look back.
    """
    end = datetime.now() - timedelta(days=1)
    start = end - timedelta(days=months * 30)
    s = start.strftime("%Y-%m-%d")
    e = end.strftime("%Y-%m-%d")

    q = f"""
        SELECT
            segments.date,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion
        FROM customer
        WHERE segments.date BETWEEN '{s}' AND '{e}'
        ORDER BY segments.date
    """
    # Aggregate by month
    monthly = {}
    for row in _query(q, customer_id or None):
        month_key = row.segments.date[:7]  # YYYY-MM
        if month_key not in monthly:
            monthly[month_key] = {"impressions": 0, "clicks": 0, "cost_micros": 0,
                                   "conversions": 0.0, "conversion_value": 0}
        monthly[month_key]["impressions"] += row.metrics.impressions
        monthly[month_key]["clicks"] += row.metrics.clicks
        monthly[month_key]["cost_micros"] += row.metrics.cost_micros
        monthly[month_key]["conversions"] += row.metrics.conversions
        monthly[month_key]["conversion_value"] += row.metrics.conversions_value

    rows = []
    for month in sorted(monthly.keys()):
        d = monthly[month]
        rows.append({
            "month": month,
            "impressions": d["impressions"],
            "clicks": d["clicks"],
            "cost": _micros(d["cost_micros"]),
            "conversions": round(d["conversions"], 2),
            "conversion_value": _value(d["conversion_value"]),
            "ctr": _pct(d["clicks"] / d["impressions"]) if d["impressions"] > 0 else 0,
            "avg_cpc": _micros(d["cost_micros"] / d["clicks"]) if d["clicks"] > 0 else 0,
            "cost_per_conversion": _cpa(d["cost_micros"], d["conversions"]),
        })
    return {"months": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  12. CONVERSION TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_conversion_actions(
    customer_id: str = "",
) -> dict:
    """
    List all conversion actions configured in the account.
    Shows type, category, status, and counting type.
    """
    q = """
        SELECT
            conversion_action.id,
            conversion_action.name,
            conversion_action.type,
            conversion_action.category,
            conversion_action.status,
            conversion_action.counting_type,
            conversion_action.value_settings.default_value,
            conversion_action.value_settings.always_use_default_value
        FROM conversion_action
        ORDER BY conversion_action.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "conversion_id": str(row.conversion_action.id),
            "name": row.conversion_action.name,
            "type": _safe_enum(row.conversion_action.type),
            "category": _safe_enum(row.conversion_action.category),
            "status": _safe_enum(row.conversion_action.status),
            "counting_type": _safe_enum(row.conversion_action.counting_type),
            "default_value": row.conversion_action.value_settings.default_value or 0,
            "always_use_default": row.conversion_action.value_settings.always_use_default_value,
        })
    return {"conversion_actions": rows, "count": len(rows)}


@mcp.tool()
def get_conversions_by_campaign(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    customer_id: str = "",
) -> dict:
    """
    Get conversion metrics aggregated by campaign.
    Includes all conversions, conversion value, and cost per conversion.
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            metrics.conversions,
            metrics.conversions_value,
            metrics.cost_micros,
            metrics.cost_per_conversion,
            metrics.all_conversions,
            metrics.all_conversions_value,
            metrics.conversions_from_interactions_rate,
            metrics.value_per_conversion
        FROM campaign
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND campaign.status = 'ENABLED'
    """
    agg = {}
    for row in _query(q, customer_id or None):
        cid = str(row.campaign.id)
        if cid not in agg:
            agg[cid] = {
                "campaign_id": cid, "name": row.campaign.name,
                "conversions": 0.0, "conversion_value": 0,
                "cost_micros": 0, "all_conversions": 0.0,
                "all_conversions_value": 0,
            }
        agg[cid]["conversions"] += row.metrics.conversions
        agg[cid]["conversion_value"] += row.metrics.conversions_value
        agg[cid]["cost_micros"] += row.metrics.cost_micros
        agg[cid]["all_conversions"] += row.metrics.all_conversions
        agg[cid]["all_conversions_value"] += row.metrics.all_conversions_value

    rows = []
    for d in agg.values():
        rows.append({
            "campaign_id": d["campaign_id"],
            "campaign_name": d["name"],
            "conversions": round(d["conversions"], 2),
            "conversion_value": _value(d["conversion_value"]),
            "cost": _micros(d["cost_micros"]),
            "cost_per_conversion": _cpa(d["cost_micros"], d["conversions"]),
            "all_conversions": round(d["all_conversions"], 2),
            "all_conversions_value": _value(d["all_conversions_value"]),
            "roas": round(d["conversion_value"] / (d["cost_micros"] / 1_000_000), 2) if d["cost_micros"] > 0 else 0,
        })
    rows.sort(key=lambda x: x["conversions"], reverse=True)
    return {"campaigns": rows, "count": len(rows)}


@mcp.tool()
def get_conversion_by_action(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    customer_id: str = "",
) -> dict:
    """
    Get conversion metrics broken down by conversion action name.
    Shows which conversion actions are driving results.
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            segments.conversion_action_name,
            segments.conversion_action_category,
            metrics.conversions,
            metrics.conversions_value,
            metrics.all_conversions,
            metrics.all_conversions_value
        FROM customer
        WHERE segments.date BETWEEN '{s}' AND '{e}'
    """
    agg = {}
    for row in _query(q, customer_id or None):
        name = row.segments.conversion_action_name or "Unknown"
        if name not in agg:
            agg[name] = {
                "category": _safe_enum(row.segments.conversion_action_category),
                "conversions": 0.0, "conversion_value": 0,
                "all_conversions": 0.0, "all_conversions_value": 0,
            }
        agg[name]["conversions"] += row.metrics.conversions
        agg[name]["conversion_value"] += row.metrics.conversions_value
        agg[name]["all_conversions"] += row.metrics.all_conversions
        agg[name]["all_conversions_value"] += row.metrics.all_conversions_value

    rows = []
    for name, d in agg.items():
        rows.append({
            "conversion_action": name,
            "category": d["category"],
            "conversions": round(d["conversions"], 2),
            "conversion_value": _value(d["conversion_value"]),
            "all_conversions": round(d["all_conversions"], 2),
            "all_conversions_value": _value(d["all_conversions_value"]),
        })
    rows.sort(key=lambda x: x["conversions"], reverse=True)
    return {"conversion_actions": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  13. BIDDING STRATEGY TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_bidding_strategies(
    customer_id: str = "",
) -> dict:
    """
    List all portfolio bidding strategies in the account.
    """
    q = """
        SELECT
            bidding_strategy.id,
            bidding_strategy.name,
            bidding_strategy.type,
            bidding_strategy.campaign_count,
            bidding_strategy.status
        FROM bidding_strategy
        ORDER BY bidding_strategy.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "strategy_id": str(row.bidding_strategy.id),
            "name": row.bidding_strategy.name,
            "type": _safe_enum(row.bidding_strategy.type),
            "campaign_count": row.bidding_strategy.campaign_count,
            "status": _safe_enum(row.bidding_strategy.status),
        })
    return {"strategies": rows, "count": len(rows)}


@mcp.tool()
def get_campaign_bid_strategy_details(
    customer_id: str = "",
) -> dict:
    """
    Get bid strategy details per campaign: type, target CPA/ROAS, enhanced CPC settings.
    """
    q = """
        SELECT
            campaign.id,
            campaign.name,
            campaign.bidding_strategy_type,
            campaign.target_cpa.target_cpa_micros,
            campaign.maximize_conversions.target_cpa_micros,
            campaign.target_roas.target_roas,
            campaign.maximize_conversion_value.target_roas,
            campaign.manual_cpc.enhanced_cpc_enabled
        FROM campaign
        WHERE campaign.status = 'ENABLED'
        ORDER BY campaign.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        target_cpa = (
            row.campaign.target_cpa.target_cpa_micros
            or row.campaign.maximize_conversions.target_cpa_micros
            or 0
        )
        target_roas = (
            row.campaign.target_roas.target_roas
            or row.campaign.maximize_conversion_value.target_roas
            or 0
        )
        rows.append({
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "bid_strategy": _safe_enum(row.campaign.bidding_strategy_type),
            "target_cpa": _micros(target_cpa),
            "target_roas": round(target_roas, 2) if target_roas else 0,
            "enhanced_cpc": row.campaign.manual_cpc.enhanced_cpc_enabled,
        })
    return {"campaigns": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  14. LANDING PAGE TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_landing_page_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    limit: int = 100,
    customer_id: str = "",
) -> dict:
    """
    Get landing page performance metrics.
    Shows how different landing pages perform in terms of clicks, conversions, etc.
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            campaign.status,
            landing_page_view.unexpanded_final_url,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM landing_page_view
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND campaign.status = 'ENABLED'
        ORDER BY metrics.clicks DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "landing_page": row.landing_page_view.unexpanded_final_url or "",
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "conversion_value": _value(row.metrics.conversions_value),
            "ctr": _pct(row.metrics.ctr),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"landing_pages": rows, "count": len(rows)}


@mcp.tool()
def get_expanded_landing_page_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    limit: int = 100,
    customer_id: str = "",
) -> dict:
    """
    Get expanded landing page performance (with full URL including parameters).
    More granular than the basic landing page view.
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            campaign.status,
            expanded_landing_page_view.expanded_final_url,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM expanded_landing_page_view
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND campaign.status = 'ENABLED'
        ORDER BY metrics.clicks DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "expanded_url": row.expanded_landing_page_view.expanded_final_url or "",
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "conversion_value": _value(row.metrics.conversions_value),
            "ctr": _pct(row.metrics.ctr),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"landing_pages": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  15. AUDIENCE TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_campaign_audience_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get audience segment performance at campaign level.
    Shows how different audience segments perform.
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.name,
            campaign_criterion.resource_name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM campaign_audience_view
        WHERE {' AND '.join(where)}
        ORDER BY metrics.cost_micros DESC
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_name": row.campaign.name,
            "audience_resource": row.campaign_criterion.resource_name,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "ctr": _pct(row.metrics.ctr),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"audiences": rows, "count": len(rows)}


@mcp.tool()
def get_ad_group_audience_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get audience segment performance at ad group level.
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.name,
            ad_group.name,
            ad_group_criterion.resource_name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.ctr,
            metrics.cost_per_conversion
        FROM ad_group_audience_view
        WHERE {' AND '.join(where)}
        ORDER BY metrics.cost_micros DESC
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "audience_resource": row.ad_group_criterion.resource_name,
            "impressions": row.metrics.impressions,
            "clicks": row.metrics.clicks,
            "cost": _micros(row.metrics.cost_micros),
            "conversions": round(row.metrics.conversions, 2),
            "ctr": _pct(row.metrics.ctr),
            "cost_per_conversion": _micros(row.metrics.cost_per_conversion),
        })
    return {"audiences": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  16. AD NETWORK TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_network_performance(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    customer_id: str = "",
) -> dict:
    """
    Get performance by ad network type (SEARCH, SEARCH_PARTNERS, CONTENT, etc.).
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            segments.ad_network_type,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.conversions_value,
            metrics.ctr,
            metrics.average_cpc,
            metrics.cost_per_conversion
        FROM campaign
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND campaign.status = 'ENABLED'
    """
    agg = {}
    for row in _query(q, customer_id or None):
        net = _safe_enum(row.segments.ad_network_type)
        if net not in agg:
            agg[net] = {"impressions": 0, "clicks": 0, "cost_micros": 0,
                        "conversions": 0.0, "conversion_value": 0}
        agg[net]["impressions"] += row.metrics.impressions
        agg[net]["clicks"] += row.metrics.clicks
        agg[net]["cost_micros"] += row.metrics.cost_micros
        agg[net]["conversions"] += row.metrics.conversions
        agg[net]["conversion_value"] += row.metrics.conversions_value

    rows = []
    for net, d in agg.items():
        rows.append({
            "network": net,
            "impressions": d["impressions"],
            "clicks": d["clicks"],
            "cost": _micros(d["cost_micros"]),
            "conversions": round(d["conversions"], 2),
            "conversion_value": _value(d["conversion_value"]),
            "ctr": _pct(d["clicks"] / d["impressions"]) if d["impressions"] > 0 else 0,
            "avg_cpc": _micros(d["cost_micros"] / d["clicks"]) if d["clicks"] > 0 else 0,
            "cost_per_conversion": _cpa(d["cost_micros"], d["conversions"]),
        })
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return {"networks": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  17. CHANGE HISTORY
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_change_history(
    start_date: str = "14daysAgo",
    end_date: str = "today",
    resource_type: str = "",
    limit: int = 100,
    customer_id: str = "",
) -> dict:
    """
    Get recent account change history.
    resource_type: filter to specific type (e.g. CAMPAIGN, AD_GROUP, AD, CRITERION). Leave empty for all.
    """
    s, e = _dates(start_date, end_date)
    # change_event uses datetime format, not date
    start_dt = f"{s} 00:00:00"
    end_dt = f"{e} 23:59:59"

    where = [
        f"change_event.change_date_time >= '{start_dt}'",
        f"change_event.change_date_time <= '{end_dt}'",
    ]
    if resource_type:
        where.append(f"change_event.change_resource_type = '{resource_type}'")

    q = f"""
        SELECT
            change_event.change_date_time,
            change_event.change_resource_type,
            change_event.change_resource_name,
            change_event.resource_change_operation,
            change_event.user_email,
            change_event.client_type,
            change_event.changed_fields
        FROM change_event
        WHERE {' AND '.join(where)}
        ORDER BY change_event.change_date_time DESC
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        changed_fields = []
        if row.change_event.changed_fields and row.change_event.changed_fields.paths:
            changed_fields = list(row.change_event.changed_fields.paths)
        rows.append({
            "timestamp": row.change_event.change_date_time,
            "resource_type": _safe_enum(row.change_event.change_resource_type),
            "resource_name": row.change_event.change_resource_name,
            "operation": _safe_enum(row.change_event.resource_change_operation),
            "user_email": row.change_event.user_email or "",
            "client_type": _safe_enum(row.change_event.client_type),
            "changed_fields": changed_fields,
        })
    return {"changes": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  18. RECOMMENDATIONS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_recommendations(
    campaign_id: str = "",
    limit: int = 50,
    customer_id: str = "",
) -> dict:
    """
    Get Google Ads recommendations for the account.
    Includes recommendation type and associated campaign info.
    """
    where = ["recommendation.resource_name IS NOT NULL"]
    if campaign_id:
        where.append(f"recommendation.campaign = 'customers/{(customer_id or CUSTOMER_ID).replace('-','')}/campaigns/{campaign_id}'")

    q = f"""
        SELECT
            recommendation.type,
            recommendation.resource_name,
            recommendation.campaign,
            recommendation.impact
        FROM recommendation
        LIMIT {limit}
    """
    rows = []
    for row in _query(q, customer_id or None):
        imp = row.recommendation.impact
        rows.append({
            "type": _safe_enum(row.recommendation.type),
            "resource_name": row.recommendation.resource_name,
            "campaign": row.recommendation.campaign or "",
            "base_impressions": imp.base_metrics.impressions if imp.base_metrics else 0,
            "base_clicks": imp.base_metrics.clicks if imp.base_metrics else 0,
            "base_cost": _micros(imp.base_metrics.cost_micros) if imp.base_metrics else 0,
            "base_conversions": round(imp.base_metrics.conversions, 2) if imp.base_metrics else 0,
            "potential_impressions": imp.potential_metrics.impressions if imp.potential_metrics else 0,
            "potential_clicks": imp.potential_metrics.clicks if imp.potential_metrics else 0,
            "potential_cost": _micros(imp.potential_metrics.cost_micros) if imp.potential_metrics else 0,
            "potential_conversions": round(imp.potential_metrics.conversions, 2) if imp.potential_metrics else 0,
        })
    return {"recommendations": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  19. LABEL TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_labels(
    customer_id: str = "",
) -> dict:
    """
    List all labels in the account.
    Labels help organize campaigns, ad groups, and keywords.
    """
    q = """
        SELECT
            label.id,
            label.name,
            label.status
        FROM label
        WHERE label.status = 'ENABLED'
        ORDER BY label.name
    """
    rows = []
    for row in _query(q, customer_id or None):
        rows.append({
            "label_id": str(row.label.id),
            "name": row.label.name,
        })
    return {"labels": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  20. AUCTION INSIGHTS (COMPETITOR ANALYSIS)
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def get_auction_insights_by_campaign(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get Auction Insights report at campaign level — shows competitor domains and their share.
    Metrics per competitor:
    - impression_share: % of impressions they received
    - overlap_rate: how often they showed alongside you
    - position_above_rate: how often they ranked above you
    - top_of_page_rate: how often they appeared at top of page
    - abs_top_of_page_rate: how often they appeared in #1 position
    - outranking_share: how often you outranked them
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")

    q = f"""
        SELECT
            campaign.id,
            campaign.name,
            segments.auction_insight_domain,
            metrics.auction_insight_search_impression_share,
            metrics.auction_insight_search_overlap_rate,
            metrics.auction_insight_search_position_above_rate,
            metrics.auction_insight_search_top_impression_percentage,
            metrics.auction_insight_search_absolute_top_impression_percentage,
            metrics.auction_insight_search_outranking_share
        FROM campaign
        WHERE {' AND '.join(where)}
    """
    rows = []
    for row in _query(q, customer_id or None):
        m = row.metrics
        rows.append({
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            "competitor_domain": row.segments.auction_insight_domain,
            "impression_share": _pct(m.auction_insight_search_impression_share),
            "overlap_rate": _pct(m.auction_insight_search_overlap_rate),
            "position_above_rate": _pct(m.auction_insight_search_position_above_rate),
            "top_of_page_rate": _pct(m.auction_insight_search_top_impression_percentage),
            "abs_top_of_page_rate": _pct(m.auction_insight_search_absolute_top_impression_percentage),
            "outranking_share": _pct(m.auction_insight_search_outranking_share),
        })
    rows.sort(key=lambda x: x["impression_share"], reverse=True)
    return {"auction_insights": rows, "count": len(rows)}


@mcp.tool()
def get_auction_insights_by_keyword(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    campaign_id: str = "",
    ad_group_id: str = "",
    customer_id: str = "",
) -> dict:
    """
    Get Auction Insights at keyword level — shows which competitors appear for each keyword.
    Filter by campaign_id and/or ad_group_id (optional).
    """
    s, e = _dates(start_date, end_date)
    where = [
        f"segments.date BETWEEN '{s}' AND '{e}'",
        "campaign.status = 'ENABLED'",
        "ad_group.status = 'ENABLED'",
        "ad_group_criterion.status = 'ENABLED'",
    ]
    if campaign_id:
        where.append(f"campaign.id = {campaign_id}")
    if ad_group_id:
        where.append(f"ad_group.id = {ad_group_id}")

    q = f"""
        SELECT
            campaign.name,
            ad_group.name,
            ad_group_criterion.keyword.text,
            segments.auction_insight_domain,
            metrics.auction_insight_search_impression_share,
            metrics.auction_insight_search_overlap_rate,
            metrics.auction_insight_search_position_above_rate,
            metrics.auction_insight_search_top_impression_percentage,
            metrics.auction_insight_search_absolute_top_impression_percentage,
            metrics.auction_insight_search_outranking_share
        FROM keyword_view
        WHERE {' AND '.join(where)}
    """
    rows = []
    for row in _query(q, customer_id or None):
        m = row.metrics
        rows.append({
            "campaign_name": row.campaign.name,
            "ad_group_name": row.ad_group.name,
            "keyword": row.ad_group_criterion.keyword.text,
            "competitor_domain": row.segments.auction_insight_domain,
            "impression_share": _pct(m.auction_insight_search_impression_share),
            "overlap_rate": _pct(m.auction_insight_search_overlap_rate),
            "position_above_rate": _pct(m.auction_insight_search_position_above_rate),
            "top_of_page_rate": _pct(m.auction_insight_search_top_impression_percentage),
            "abs_top_of_page_rate": _pct(m.auction_insight_search_absolute_top_impression_percentage),
            "outranking_share": _pct(m.auction_insight_search_outranking_share),
        })
    rows.sort(key=lambda x: (x["keyword"], -x["impression_share"]))
    return {"auction_insights": rows, "count": len(rows)}


@mcp.tool()
def get_top_competitors(
    start_date: str = "30daysAgo",
    end_date: str = "yesterday",
    customer_id: str = "",
) -> dict:
    """
    Get a summary of top competitors across all campaigns.
    Aggregates auction insights to show who your biggest competitors are overall.
    Returns competitors ranked by average impression share.
    """
    s, e = _dates(start_date, end_date)
    q = f"""
        SELECT
            segments.auction_insight_domain,
            metrics.auction_insight_search_impression_share,
            metrics.auction_insight_search_overlap_rate,
            metrics.auction_insight_search_position_above_rate,
            metrics.auction_insight_search_top_impression_percentage,
            metrics.auction_insight_search_absolute_top_impression_percentage,
            metrics.auction_insight_search_outranking_share
        FROM campaign
        WHERE segments.date BETWEEN '{s}' AND '{e}'
            AND campaign.status = 'ENABLED'
    """
    # Aggregate by domain
    agg = {}
    for row in _query(q, customer_id or None):
        domain = row.segments.auction_insight_domain
        m = row.metrics
        if domain not in agg:
            agg[domain] = {
                "impression_share": [],
                "overlap_rate": [],
                "position_above_rate": [],
                "top_of_page_rate": [],
                "abs_top_of_page_rate": [],
                "outranking_share": [],
            }
        agg[domain]["impression_share"].append(m.auction_insight_search_impression_share or 0)
        agg[domain]["overlap_rate"].append(m.auction_insight_search_overlap_rate or 0)
        agg[domain]["position_above_rate"].append(m.auction_insight_search_position_above_rate or 0)
        agg[domain]["top_of_page_rate"].append(m.auction_insight_search_top_impression_percentage or 0)
        agg[domain]["abs_top_of_page_rate"].append(m.auction_insight_search_absolute_top_impression_percentage or 0)
        agg[domain]["outranking_share"].append(m.auction_insight_search_outranking_share or 0)

    def _avg(lst):
        return round(sum(lst) / len(lst) * 100, 2) if lst else 0

    rows = []
    for domain, d in agg.items():
        rows.append({
            "competitor_domain": domain,
            "avg_impression_share": _avg(d["impression_share"]),
            "avg_overlap_rate": _avg(d["overlap_rate"]),
            "avg_position_above_rate": _avg(d["position_above_rate"]),
            "avg_top_of_page_rate": _avg(d["top_of_page_rate"]),
            "avg_abs_top_of_page_rate": _avg(d["abs_top_of_page_rate"]),
            "avg_outranking_share": _avg(d["outranking_share"]),
            "data_points": len(d["impression_share"]),
        })
    rows.sort(key=lambda x: x["avg_impression_share"], reverse=True)
    return {"competitors": rows, "count": len(rows)}


# ═════════════════════════════════════════════════════════════════════════════
#  21. MUTATION / ACTION TOOLS
# ═════════════════════════════════════════════════════════════════════════════


@mcp.tool()
def add_negative_keyword(
    campaign_id: str,
    keyword: str,
    match_type: str = "BROAD",
    customer_id: str = "",
) -> dict:
    """
    Add a negative keyword to a campaign.
    match_type: BROAD, PHRASE, EXACT (default: BROAD).

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")

    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operation = client.get_type("CampaignCriterionOperation")
    criterion = operation.create
    criterion.campaign = client.get_service("CampaignService").campaign_path(cid, campaign_id)
    criterion.negative = True
    criterion.keyword.text = keyword
    criterion.keyword.match_type = getattr(client.enums.KeywordMatchTypeEnum, match_type, client.enums.KeywordMatchTypeEnum.BROAD)

    response = campaign_criterion_service.mutate_campaign_criteria(
        customer_id=cid, operations=[operation]
    )
    return {
        "status": "success",
        "resource_name": response.results[0].resource_name,
        "keyword": keyword,
        "match_type": match_type,
        "campaign_id": campaign_id,
    }


@mcp.tool()
def remove_negative_keyword(
    criterion_resource_name: str,
    customer_id: str = "",
) -> dict:
    """
    Remove a negative keyword using its resource name.
    Get the resource_name from get_negative_keywords tool.

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")

    campaign_criterion_service = client.get_service("CampaignCriterionService")
    operation = client.get_type("CampaignCriterionOperation")
    operation.remove = criterion_resource_name

    campaign_criterion_service.mutate_campaign_criteria(
        customer_id=cid, operations=[operation]
    )
    return {"status": "success", "removed": criterion_resource_name}


@mcp.tool()
def update_campaign_budget(
    campaign_id: str,
    new_daily_budget: float,
    customer_id: str = "",
) -> dict:
    """
    Update a campaign's daily budget.
    new_daily_budget: new budget amount in USD (e.g., 50.00 for $50/day).

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")
    ga_service = client.get_service("GoogleAdsService")

    # Find current budget resource
    q = f"SELECT campaign.campaign_budget FROM campaign WHERE campaign.id = {campaign_id}"
    budget_resource = None
    for row in ga_service.search(customer_id=cid, query=q):
        budget_resource = row.campaign.campaign_budget

    if not budget_resource:
        return {"status": "error", "error": f"Budget resource not found for campaign {campaign_id}"}

    budget_service = client.get_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.update
    budget.resource_name = budget_resource
    budget.amount_micros = int(new_daily_budget * 1_000_000)

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("amount_micros")
    operation.update_mask.CopyFrom(field_mask)

    budget_service.mutate_campaign_budgets(customer_id=cid, operations=[operation])
    return {
        "status": "success",
        "campaign_id": campaign_id,
        "new_daily_budget": new_daily_budget,
    }


@mcp.tool()
def pause_campaign(
    campaign_id: str,
    customer_id: str = "",
) -> dict:
    """
    Pause a campaign.

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")

    campaign_service = client.get_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = campaign_service.campaign_path(cid, campaign_id)
    campaign.status = client.enums.CampaignStatusEnum.PAUSED

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("status")
    operation.update_mask.CopyFrom(field_mask)

    campaign_service.mutate_campaigns(customer_id=cid, operations=[operation])
    return {"status": "success", "campaign_id": campaign_id, "new_status": "PAUSED"}


@mcp.tool()
def enable_campaign(
    campaign_id: str,
    customer_id: str = "",
) -> dict:
    """
    Enable (unpause) a campaign.

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")

    campaign_service = client.get_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.update
    campaign.resource_name = campaign_service.campaign_path(cid, campaign_id)
    campaign.status = client.enums.CampaignStatusEnum.ENABLED

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("status")
    operation.update_mask.CopyFrom(field_mask)

    campaign_service.mutate_campaigns(customer_id=cid, operations=[operation])
    return {"status": "success", "campaign_id": campaign_id, "new_status": "ENABLED"}


@mcp.tool()
def pause_ad_group(
    ad_group_id: str,
    customer_id: str = "",
) -> dict:
    """
    Pause an ad group.

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")

    ad_group_service = client.get_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = ad_group_service.ad_group_path(cid, ad_group_id)
    ad_group.status = client.enums.AdGroupStatusEnum.PAUSED

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("status")
    operation.update_mask.CopyFrom(field_mask)

    ad_group_service.mutate_ad_groups(customer_id=cid, operations=[operation])
    return {"status": "success", "ad_group_id": ad_group_id, "new_status": "PAUSED"}


@mcp.tool()
def enable_ad_group(
    ad_group_id: str,
    customer_id: str = "",
) -> dict:
    """
    Enable (unpause) an ad group.

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")

    ad_group_service = client.get_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.update
    ad_group.resource_name = ad_group_service.ad_group_path(cid, ad_group_id)
    ad_group.status = client.enums.AdGroupStatusEnum.ENABLED

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("status")
    operation.update_mask.CopyFrom(field_mask)

    ad_group_service.mutate_ad_groups(customer_id=cid, operations=[operation])
    return {"status": "success", "ad_group_id": ad_group_id, "new_status": "ENABLED"}


@mcp.tool()
def pause_keyword(
    ad_group_id: str,
    keyword_id: str,
    customer_id: str = "",
) -> dict:
    """
    Pause a keyword in an ad group.

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")

    criterion_service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = criterion_service.ad_group_criterion_path(cid, ad_group_id, keyword_id)
    criterion.status = client.enums.AdGroupCriterionStatusEnum.PAUSED

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("status")
    operation.update_mask.CopyFrom(field_mask)

    criterion_service.mutate_ad_group_criteria(customer_id=cid, operations=[operation])
    return {"status": "success", "ad_group_id": ad_group_id, "keyword_id": keyword_id, "new_status": "PAUSED"}


@mcp.tool()
def enable_keyword(
    ad_group_id: str,
    keyword_id: str,
    customer_id: str = "",
) -> dict:
    """
    Enable (unpause) a keyword in an ad group.

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")

    criterion_service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = criterion_service.ad_group_criterion_path(cid, ad_group_id, keyword_id)
    criterion.status = client.enums.AdGroupCriterionStatusEnum.ENABLED

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("status")
    operation.update_mask.CopyFrom(field_mask)

    criterion_service.mutate_ad_group_criteria(customer_id=cid, operations=[operation])
    return {"status": "success", "ad_group_id": ad_group_id, "keyword_id": keyword_id, "new_status": "ENABLED"}


@mcp.tool()
def adjust_keyword_bid(
    ad_group_id: str,
    keyword_id: str,
    new_bid: float,
    customer_id: str = "",
) -> dict:
    """
    Adjust the CPC bid for a keyword.
    new_bid: new bid amount in USD (e.g., 2.50 for $2.50).

    ⚠️ CAUTION: This modifies your Google Ads account.
    """
    client = _client()
    cid = (customer_id or CUSTOMER_ID).replace("-", "")

    criterion_service = client.get_service("AdGroupCriterionService")
    operation = client.get_type("AdGroupCriterionOperation")
    criterion = operation.update
    criterion.resource_name = criterion_service.ad_group_criterion_path(cid, ad_group_id, keyword_id)
    criterion.cpc_bid_micros = int(new_bid * 1_000_000)

    field_mask = client.get_type("FieldMask")
    field_mask.paths.append("cpc_bid_micros")
    operation.update_mask.CopyFrom(field_mask)

    criterion_service.mutate_ad_group_criteria(customer_id=cid, operations=[operation])
    return {
        "status": "success",
        "ad_group_id": ad_group_id,
        "keyword_id": keyword_id,
        "new_bid": new_bid,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run()
