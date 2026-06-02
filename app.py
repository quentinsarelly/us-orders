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

            order_date = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))

            tags = order.get("tags", "")
            results.append({
                "order_num": order.get("name", ""),
                "tiktok_order": extract_tiktok_order(tags),
                "shopify_tracking": _shopify_tracking(order.get("fulfillments", [])),
                "created_at": order_date.astimezone(us_tz).strftime("%Y-%m-%d"),
                "shipped_by_tiktok": "shipped by tiktok" in tags.lower(),
            })
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

_AWAITING_STATUSES = {"CHARGED", "COMMITTED", "PICK PRTD"}

def _issue_label(camelot_result: dict, shopify_tracking: str, shipped_by_tiktok: bool = False) -> str:
    if camelot_result.get("error") is not None:
        if shipped_by_tiktok:
            return "🟢 Shipped by TikTok"
        return "🔴 Order not transmitted to Camelot"

    status = camelot_result.get("status") or {}
    camelot_status = status.get("status", "").upper()
    camelot_tracking = status.get("tracking_number", "")

    if camelot_status in _AWAITING_STATUSES:
        return "🟠 Awaiting picking"

    if camelot_status == "SHIPPED-NOPACKITEM":
        if camelot_tracking and not shopify_tracking:
            return "🔴 Tracking not pushed back to Shopify"
        if not camelot_tracking:
            return "🟡 Shipped but no Camelot tracking"
        if camelot_tracking and shopify_tracking:
            return "🟢 Shipped & synced"

    return ""


# ---------- CSV loader helper ----------

def _load_csv_upload(file_upload, key_prefix: str) -> dict | None:
    """Render column selectors for an uploaded file and return a lookup dict, or None if no file."""
    if file_upload is None:
        return None
    try:
        if file_upload.name.endswith((".xlsx", ".xls")):
            df = pd.read_excel(file_upload, dtype=str)
        else:
            raw = file_upload.read()
            for enc in ("utf-8-sig", "latin-1", "cp1252", "utf-16"):
                try:
                    import io
                    df = pd.read_csv(io.BytesIO(raw), dtype=str, encoding=enc)
                    break
                except (UnicodeDecodeError, Exception):
                    continue
            else:
                st.error("Could not detect file encoding. Try saving the CSV as UTF-8 from Excel.")
                st.stop()
    except Exception as e:
        st.error(f"Could not read file: {e}")
        st.stop()

    st.success(f"{len(df)} rows loaded — {len(df.columns)} columns detected.")
    cols = list(df.columns)

    def _best(keywords):
        for k in keywords:
            m = next((c for c in cols if k in c.lower()), None)
            if m:
                return cols.index(m)
        return 0

    c1, c2, c3 = st.columns(3)
    with c1:
        ref_col = st.selectbox("Reference column (= Shopify order #)", cols,
                               index=_best(["ref", "your ref", "external", "order no", "order #"]),
                               key=f"{key_prefix}_ref")
    with c2:
        tracking_col = st.selectbox("Tracking number column", cols,
                                    index=_best(["track"]), key=f"{key_prefix}_tracking")
    with c3:
        status_options = ["(none)"] + cols
        status_col = st.selectbox("Status column (optional)", status_options,
                                  index=_best(["status"]) + 1, key=f"{key_prefix}_status")

    st.dataframe(
        df[[ref_col, tracking_col] + ([status_col] if status_col != "(none)" else [])].head(10),
        use_container_width=True, hide_index=True,
    )
    return build_camelot_lookup(df, ref_col, tracking_col,
                                status_col if status_col != "(none)" else None)


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
    ["API", "CSV upload"],
    horizontal=True,
)

camelot_lookup = None        # populated in CSV mode
pending_lookup: dict = {}    # optional pending orders CSV (API mode)

if camelot_source == "CSV upload":
    st.caption("Export shipped orders from the Camelot / Excalibur UI and upload below.")
    camelot_lookup = _load_csv_upload(
        st.file_uploader("Camelot shipped orders export", type=["csv", "xlsx", "xls"], key="shipped_csv"),
        key_prefix="shipped",
    )

else:
    with st.expander("Upload pending orders CSV (optional)", expanded=False):
        st.caption("Export pending/unshipped orders from the Camelot web UI and upload below. "
                   "These will be merged with the API shipped orders so they aren't flagged as missing.")
        pending_lookup = _load_csv_upload(
            st.file_uploader("Camelot pending orders export", type=["csv", "xlsx", "xls"], key="pending_csv"),
            key_prefix="pending",
        ) or {}

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
        with st.spinner("Fetching Camelot shipped orders…"):
            camelot_results = check_camelot_api(orders, camelot, start, end)
        # Merge pending orders — pending entries don't overwrite shipped ones
        for ref, data in pending_lookup.items():
            for num in [ref, ref.lstrip("#"), "#" + ref.lstrip("#")]:
                if num in camelot_results and camelot_results[num].get("error") is None:
                    break  # already found as shipped
            else:
                camelot_results[ref] = {"status": data, "error": None}

    rows = []
    issues_count = {"not_transmitted": 0, "tracking_not_pushed": 0, "awaiting": 0, "no_tracking": 0, "synced": 0}

    for o in orders:
        num = o["order_num"]
        cr = camelot_results.get(num, {"status": None, "error": "No result"})
        status = cr.get("status") or {}
        error = cr.get("error")

        camelot_tracking = status.get("tracking_number", "") if status else ""
        camelot_status = status.get("status", "") if status else ""
        shopify_tracking = o["shopify_tracking"]
        issue = _issue_label(cr, shopify_tracking, o.get("shipped_by_tiktok", False))

        if issue == "🔴 Order not transmitted to Camelot":
            issues_count["not_transmitted"] += 1
        elif issue == "🔴 Tracking not pushed back to Shopify":
            issues_count["tracking_not_pushed"] += 1
        elif issue == "🟠 Awaiting picking":
            issues_count["awaiting"] += 1
        elif issue == "🟡 Shipped but no Camelot tracking":
            issues_count["no_tracking"] += 1
        elif issue in ("🟢 Shipped & synced", "🟢 Shipped by TikTok"):
            issues_count["synced"] += 1

        rows.append({
            "Order Date": o["created_at"],
            "Order #": num,
            "TikTok Order #": o["tiktok_order"],
            "Camelot Status": camelot_status if not error else "Not found",
            "Camelot Tracking": camelot_tracking,
            "Shopify Tracking": shopify_tracking,
            "Issue": issue,
        })

    st.session_state["rows"] = rows
    st.session_state["issues_count"] = issues_count

if "rows" in st.session_state:
    rows = st.session_state["rows"]
    issues_count = st.session_state["issues_count"]

    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("Total Orders", len(rows))
    m2.metric("🟢 Shipped & synced", issues_count["synced"])
    m3.metric("🟠 Awaiting picking", issues_count["awaiting"])
    m4.metric("🔴 Not transmitted", issues_count["not_transmitted"],
              delta=f"⚠ {issues_count['not_transmitted']}" if issues_count["not_transmitted"] else None,
              delta_color="inverse")
    m5.metric("🔴 Tracking not pushed", issues_count["tracking_not_pushed"],
              delta=f"⚠ {issues_count['tracking_not_pushed']}" if issues_count["tracking_not_pushed"] else None,
              delta_color="inverse")
    m6.metric("🟡 No Camelot tracking", issues_count["no_tracking"],
              delta=f"⚠ {issues_count['no_tracking']}" if issues_count["no_tracking"] else None,
              delta_color="inverse")

    st.divider()

    show_issues_only = st.checkbox("Show issues only", value=False)
    display_rows = [r for r in rows if r["Issue"] and "🟢" not in r["Issue"]] if show_issues_only else rows

    if not display_rows:
        st.success("No sync issues found.")
    else:
        st.dataframe(
            display_rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Order Date": st.column_config.TextColumn(width="small"),
                "Order #": st.column_config.TextColumn(width="small"),
                "TikTok Order #": st.column_config.TextColumn(width="medium"),
                "Camelot Status": st.column_config.TextColumn(width="small"),
                "Camelot Tracking": st.column_config.TextColumn(width="medium"),
                "Shopify Tracking": st.column_config.TextColumn(width="medium"),
                "Issue": st.column_config.TextColumn(width="medium"),
            },
        )
