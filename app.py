import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytz
import requests
import streamlit as st

from camelot import CamelotClient, CamelotError

st.set_page_config(page_title="US Orders Sync Check", page_icon="🛒", layout="wide")

TIKTOK_TAG_PREFIX = "tiktok"
API_VERSION = "2026-01"
TOKEN_FILE = os.path.join(os.path.dirname(__file__), "tokens_us.json")


# ---------- Token management ----------

def _load_token() -> str | None:
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE) as f:
                return json.load(f).get("access_token")
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _save_token(token: str):
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump({"access_token": token}, f, indent=2)
    except OSError:
        pass


def _refresh_token(shop_domain: str, client_id: str, client_secret: str) -> str:
    url = f"https://{shop_domain}.myshopify.com/admin/oauth/access_token"
    resp = requests.post(url, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
    })
    if resp.status_code != 200:
        raise RuntimeError(f"Token refresh failed ({resp.status_code}): {resp.text}")
    token = resp.json()["access_token"]
    _save_token(token)
    return token


# ---------- Shopify API ----------

def _get(shop_domain, client_id, client_secret, token_holder, endpoint, params, _retried=False):
    url = f"https://{shop_domain}.myshopify.com/admin/api/{API_VERSION}/{endpoint}"
    headers = {"X-Shopify-Access-Token": token_holder[0], "Content-Type": "application/json"}
    resp = requests.get(url, headers=headers, params=params)

    if resp.status_code == 429:
        time.sleep(int(resp.headers.get("Retry-After", 1)))
        return _get(shop_domain, client_id, client_secret, token_holder, endpoint, params, _retried)

    if resp.status_code == 401 and not _retried:
        new_token = _refresh_token(shop_domain, client_id, client_secret)
        token_holder[0] = new_token
        return _get(shop_domain, client_id, client_secret, token_holder, endpoint, params, _retried=True)

    resp.raise_for_status()
    return resp.json()


def extract_tiktok_order(tags_str: str) -> str:
    for tag in tags_str.split(","):
        tag = tag.strip()
        if tag.lower().startswith(TIKTOK_TAG_PREFIX):
            return tag.split(":", 1)[-1]
    return ""


def _shopify_tracking(fulfillments: list) -> str:
    for f in fulfillments:
        tn = f.get("tracking_number") or ""
        if tn:
            return tn
    return ""


def fetch_orders(shop_domain, client_id, client_secret, start: date, end: date) -> list[dict]:
    token = _load_token() or _refresh_token(shop_domain, client_id, client_secret)
    token_holder = [token]

    us_tz = pytz.timezone("America/New_York")
    start_dt = us_tz.localize(datetime(start.year, start.month, start.day, 0, 0, 0))
    end_dt = us_tz.localize(datetime(end.year, end.month, end.day, 23, 59, 59))

    params = {
        "limit": 250,
        "created_at_min": start_dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "created_at_max": end_dt.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "any",
        "fields": "id,name,tags,created_at,fulfillments",
    }

    results = []
    seen_ids = set()

    while True:
        data = _get(shop_domain, client_id, client_secret, token_holder, "orders.json", params)
        batch = data.get("orders", [])
        if not batch:
            break

        oldest_dt = None
        for order in batch:
            oid = order.get("id")
            if oid in seen_ids:
                continue
            seen_ids.add(oid)

            results.append({
                "order_num": order.get("name", ""),
                "tiktok_order": extract_tiktok_order(order.get("tags", "")),
                "shopify_tracking": _shopify_tracking(order.get("fulfillments", [])),
            })

            order_date = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
            if oldest_dt is None or order_date < oldest_dt:
                oldest_dt = order_date

        if oldest_dt is None or oldest_dt <= start_dt.astimezone(timezone.utc):
            break

        params["created_at_max"] = (oldest_dt - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return results


# ---------- Camelot — API ----------

def check_camelot_api(orders: list[dict], camelot: CamelotClient, start: date, end: date) -> dict:
    """Fetch Camelot orders and match by reference number.
    Camelot filters by ship date, Shopify by order creation date — these can differ by weeks.
    We query Camelot from 30 days before start to capture orders placed earlier but shipped later.
    """
    begin = (start - timedelta(days=7)).strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    shipped = camelot.get_shipped_orders(begin, end_str)

    results = {}
    for o in orders:
        num = o["order_num"]
        match = _find_in_lookup(num, shipped)
        if match is not None:
            results[num] = {"status": match, "error": None}
        else:
            results[num] = {"status": None, "error": "Not found in Camelot"}
    return results


# ---------- Camelot — CSV ----------

def _clean(val) -> str:
    s = str(val).strip() if pd.notna(val) else ""
    return "" if s in ("nan", "None", "NaN") else s


def build_camelot_lookup(df: pd.DataFrame, ref_col: str, tracking_col: str, status_col: str | None) -> dict:
    """Build ref→data lookup. When duplicates exist, prefer the row with a tracking number."""
    lookup = {}
    for _, row in df.iterrows():
        ref = _clean(row[ref_col])
        if not ref:
            continue
        tracking = _clean(row[tracking_col])
        status = _clean(row[status_col]) if status_col else ""
        entry = {"tracking_number": tracking, "status": status}
        # Keep row with tracking over row without
        if ref not in lookup or (tracking and not lookup[ref]["tracking_number"]):
            lookup[ref] = entry
    return lookup


def _find_in_lookup(order_num: str, lookup: dict) -> dict | None:
    """Match order number against lookup, trying minor normalization variants."""
    candidates = [
        order_num,                           # exact: #SSUS52346
        order_num.lstrip("#"),               # without #: SSUS52346
        "#" + order_num.lstrip("#"),         # ensure # prefix
        order_num.upper(),
        order_num.lower(),
    ]
    for c in candidates:
        if c in lookup:
            return lookup[c]
    return None


def check_camelot_csv(orders: list[dict], lookup: dict) -> dict:
    results = {}
    for o in orders:
        num = o["order_num"]
        match = _find_in_lookup(num, lookup)
        if match is not None:
            results[num] = {"status": match, "error": None}
        else:
            results[num] = {"status": None, "error": "Not found in Camelot export"}
    return results


# ---------- Issue detection ----------

def _issue_label(camelot_result: dict, shopify_tracking: str) -> str:
    error = camelot_result.get("error")
    status = camelot_result.get("status") or {}

    if error is not None:
        return "🔴 Not in Camelot"

    camelot_tracking = status.get("tracking_number", "") if status else ""
    if camelot_tracking and not shopify_tracking:
        return "🟡 Tracking missing in Shopify"

    return ""


# ---------- UI ----------

st.title("US Orders — Shopify / Camelot Sync Check")

col1, col2 = st.columns(2)
with col1:
    start = st.date_input("Start date", value=date.today() - timedelta(days=7))
with col2:
    end = st.date_input("End date", value=date.today())

if start > end:
    st.error("Start date must be before end date.")
    st.stop()

st.divider()

# --- Camelot source ---
st.subheader("Camelot data source")
camelot_source = st.radio(
    "How to pull Camelot order data?",
    ["CSV upload", "API"],
    horizontal=True,
    help="API date format issue is pending Camelot support — use CSV export in the meantime.",
)

camelot_lookup = None  # populated if CSV mode

if camelot_source == "CSV upload":
    st.caption("Export your orders from the Camelot / Excalibur UI and upload the file below.")
    uploaded = st.file_uploader("Camelot order export", type=["csv", "xlsx", "xls"])

    if uploaded is not None:
        try:
            if uploaded.name.endswith((".xlsx", ".xls")):
                df_cam = pd.read_excel(uploaded, dtype=str)
            else:
                raw = uploaded.read()
                for enc in ("utf-8-sig", "latin-1", "cp1252", "utf-16"):
                    try:
                        df_cam = pd.read_csv(
                            __import__("io").BytesIO(raw), dtype=str, encoding=enc
                        )
                        break
                    except (UnicodeDecodeError, Exception):
                        continue
                else:
                    st.error("Could not detect file encoding. Try saving the CSV as UTF-8 from Excel.")
                    st.stop()
        except Exception as e:
            st.error(f"Could not read file: {e}")
            st.stop()

        st.success(f"{len(df_cam)} rows loaded — {len(df_cam.columns)} columns detected.")

        cols = list(df_cam.columns)

        def _best(keywords):
            for k in keywords:
                match = next((c for c in cols if k in c.lower()), None)
                if match:
                    return cols.index(match)
            return 0

        mc1, mc2, mc3 = st.columns(3)
        with mc1:
            ref_col = st.selectbox(
                "Reference column (= Shopify order #)",
                cols, index=_best(["ref", "your ref", "external", "order no", "order #"]),
            )
        with mc2:
            tracking_col = st.selectbox(
                "Tracking number column",
                cols, index=_best(["track"]),
            )
        with mc3:
            status_options = ["(none)"] + cols
            status_col = st.selectbox(
                "Status column (optional)",
                status_options, index=_best(["status"]) + 1,
            )

        st.dataframe(
            df_cam[[ref_col, tracking_col] + ([status_col] if status_col != "(none)" else [])].head(10),
            use_container_width=True, hide_index=True,
        )

        camelot_lookup = build_camelot_lookup(
            df_cam, ref_col, tracking_col,
            status_col if status_col != "(none)" else None,
        )

st.divider()

if st.button("Fetch & Check Sync", type="primary"):
    if camelot_source == "CSV upload" and camelot_lookup is None:
        st.error("Please upload a Camelot CSV export before running the check.")
        st.stop()

    shop_domain = st.secrets["SHOP_DOMAIN"]
    client_id = st.secrets["CLIENT_ID"]
    client_secret = st.secrets["CLIENT_SECRET"]

    with st.spinner("Fetching Shopify orders…"):
        try:
            orders = fetch_orders(shop_domain, client_id, client_secret, start, end)
        except Exception as e:
            st.error(f"Shopify error: {e}")
            st.stop()

    if not orders:
        st.warning("No orders found for the selected date range.")
        st.stop()

    st.info(f"{len(orders)} orders fetched from Shopify.")

    if camelot_source == "CSV upload":
        camelot_results = check_camelot_csv(orders, camelot_lookup)
    else:
        camelot = CamelotClient(
            soap_url=st.secrets["CAMELOT_SOAP_URL"],
            username=st.secrets["CAMELOT_USERNAME"],
            password=st.secrets["CAMELOT_PASSWORD"],
            interface_profile=st.secrets["CAMELOT_INTERFACE_PROFILE"],
            client_code=st.secrets["CAMELOT_CLIENT"],
            trading_partner=st.secrets["CAMELOT_TRADING_PARTNER"],
        )
        with st.spinner("Fetching Camelot orders…"):
            camelot_results = check_camelot_api(orders, camelot, start, end)

    rows = []
    issues_count = {"not_in_camelot": 0, "tracking_missing": 0}

    for o in orders:
        num = o["order_num"]
        cr = camelot_results.get(num, {"status": None, "error": "No result"})
        status = cr.get("status") or {}
        error = cr.get("error")

        camelot_tracking = status.get("tracking_number", "") if status else ""
        camelot_status = status.get("status", "") if status else ""
        shopify_tracking = o["shopify_tracking"]
        issue = _issue_label(cr, shopify_tracking)

        if issue == "🔴 Not in Camelot":
            issues_count["not_in_camelot"] += 1
        elif issue == "🟡 Tracking missing in Shopify":
            issues_count["tracking_missing"] += 1

        rows.append({
            "Order #": num,
            "TikTok Order #": o["tiktok_order"],
            "Camelot Status": camelot_status if not error else f"Not found",
            "Camelot Tracking": camelot_tracking,
            "Shopify Tracking": shopify_tracking,
            "Issue": issue,
        })

    m1, m2, m3 = st.columns(3)
    m1.metric("Total Orders", len(rows))
    m2.metric("Not in Camelot", issues_count["not_in_camelot"],
              delta=f"⚠ {issues_count['not_in_camelot']}" if issues_count["not_in_camelot"] else None,
              delta_color="inverse")
    m3.metric("Tracking Missing in Shopify", issues_count["tracking_missing"],
              delta=f"⚠ {issues_count['tracking_missing']}" if issues_count["tracking_missing"] else None,
              delta_color="inverse")

    st.divider()

    show_issues_only = st.checkbox("Show issues only", value=False)
    display_rows = [r for r in rows if r["Issue"]] if show_issues_only else rows

    if not display_rows:
        st.success("No sync issues found.")
    else:
        st.dataframe(
            display_rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Order #": st.column_config.TextColumn(width="small"),
                "TikTok Order #": st.column_config.TextColumn(width="medium"),
                "Camelot Status": st.column_config.TextColumn(width="small"),
                "Camelot Tracking": st.column_config.TextColumn(width="medium"),
                "Shopify Tracking": st.column_config.TextColumn(width="medium"),
                "Issue": st.column_config.TextColumn(width="medium"),
            },
        )
