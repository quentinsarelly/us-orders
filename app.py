import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import pytz
import requests
import streamlit as st

st.set_page_config(page_title="US Orders", page_icon="🛒", layout="centered")

# Adjust to match the actual TikTok tag format in the US store
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


# ---------- API request ----------

def _get(shop_domain: str, client_id: str, client_secret: str,
         token_holder: list, endpoint: str, params: dict, _retried=False) -> dict:
    url = f"https://{shop_domain}.myshopify.com/admin/api/{API_VERSION}/{endpoint}"
    headers = {
        "X-Shopify-Access-Token": token_holder[0],
        "Content-Type": "application/json",
    }
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


# ---------- Order fetching ----------

def extract_tiktok_order(tags_str: str) -> str:
    for tag in tags_str.split(","):
        tag = tag.strip()
        if tag.lower().startswith(TIKTOK_TAG_PREFIX):
            return tag.split(":", 1)[-1]
    return ""


def fetch_orders(shop_domain: str, client_id: str, client_secret: str,
                 start: date, end: date) -> list[dict]:
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
        "fields": "id,name,tags,created_at",
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
                "Order #": order.get("name", "").lstrip("#"),
                "TikTok Order #": extract_tiktok_order(order.get("tags", "")),
            })

            order_date = datetime.fromisoformat(order["created_at"].replace("Z", "+00:00"))
            if oldest_dt is None or order_date < oldest_dt:
                oldest_dt = order_date

        if oldest_dt is None or oldest_dt <= start_dt.astimezone(timezone.utc):
            break

        params["created_at_max"] = (oldest_dt - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    return results


# ---------- UI ----------

st.title("US Shopify Orders")

col1, col2 = st.columns(2)
with col1:
    start = st.date_input("Start date", value=date.today() - timedelta(days=7))
with col2:
    end = st.date_input("End date", value=date.today())

if start > end:
    st.error("Start date must be before end date.")
    st.stop()

if st.button("Fetch Orders", type="primary"):
    shop_domain = st.secrets["SHOP_DOMAIN"]
    client_id = st.secrets["CLIENT_ID"]
    client_secret = st.secrets["CLIENT_SECRET"]

    with st.spinner("Fetching orders..."):
        try:
            orders = fetch_orders(shop_domain, client_id, client_secret, start, end)
        except Exception as e:
            st.error(f"Error fetching orders: {e}")
            st.stop()

    if not orders:
        st.warning("No orders found for the selected date range.")
    else:
        st.success(f"{len(orders)} orders found.")
        st.dataframe(orders, width='stretch')
