"""
MSP Judge price checker.
Reads products from the Google Sheet, checks live prices on Amazon.in and
Flipkart, writes a fresh snapshot to the `results` worksheet, and keeps the
`tracker` worksheet of open violations up to date.

Amazon parent ASINs (size/colour selector pages with no price) are resolved
automatically: each child variation is checked as its own listing.

Runs headless on a schedule (GitHub Actions) with credentials supplied via
environment variables:
    GCP_SERVICE_ACCOUNT  full JSON of the Google service-account key
    SHEET_ID             the spreadsheet id
    SERVICE_ACCOUNT_FILE (optional, local runs) path to the key file instead
"""

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

IST = timezone(timedelta(hours=5, minutes=30))

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

PRODUCT_COLS = ["Brand", "SKU", "Product Name", "ASIN", "FSN", "MSP"]
RESULT_COLS = ["Run Time", "Brand", "SKU", "Product", "Marketplace", "ID",
               "MSP", "Seller", "Price", "Buy Box", "Below MSP", "Status"]
TRACKER_COLS = ["Brand", "Product", "Marketplace", "Sellers", "Lowest Seen",
                "MSP", "First Seen", "Runs Seen", "Status"]

MAX_VARIATIONS = 8  # children checked per parent ASIN

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) "
    "Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


# When SCRAPER_API_KEY is set (cloud runs), requests are routed through
# ScraperAPI so they arrive from ordinary Indian residential connections
# instead of easily-blocked datacenter IPs. Without the key (local runs),
# requests go direct.
SCRAPER_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()


def http_get(url):
    if SCRAPER_KEY:
        return requests.get(
            "https://api.scraperapi.com/",
            params={"api_key": SCRAPER_KEY, "url": url,
                    "country_code": "in"},
            timeout=90,
        )
    return requests.get(
        url,
        headers={
            "User-Agent": random.choice(UA_POOL),
            "Accept": "text/html,application/xhtml+xml,application/xml;"
                      "q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Cache-Control": "no-cache",
        },
        timeout=25,
    )


def to_number(txt):
    try:
        return float(str(txt).replace(",", "").replace("₹", "").strip())
    except (TypeError, ValueError):
        return None


def first_match(html, patterns, flags=0):
    for p in patterns:
        m = re.search(p, html, flags)
        if m:
            return m.group(1)
    return None


def polite_pause():
    time.sleep(random.uniform(4, 8))


def with_retry(fn, pid):
    """One gentle retry after a pause if the site throttled us."""
    res = fn(pid)
    if res["status"] == "UNVERIFIED (BLOCKED)":
        time.sleep(random.uniform(25, 40))
        res = fn(pid)
    return res


# --------------------------------------------------------------- amazon
def amazon_seller(html, low):
    # seller data is often HTML-escaped inside embedded JSON — unescape first
    plain = html.replace("\\&quot;", '"').replace("&quot;", '"') \
                .replace("&amp;", "&")
    seller = first_match(plain, [
        r'id="sellerProfileTriggerId"[^>]*>\s*([^<]+?)\s*<',
        r'id="tabular-buybox-text-soldBy".{0,500}?<span[^>]*>\s*'
        r'([^<]+?)\s*</span>',
        r'"merchantName"\s*:\s*"([^"]+)"',
    ], flags=re.S)
    if not seller:
        m = re.search(r'id="merchant-info"[^>]*>(.{0,400}?)</div>',
                      plain, re.S)
        if m:
            s = re.search(r'sold by\s*(?:<[^>]+>)*\s*([^<.]+)',
                          m.group(1), re.I)
            if s:
                seller = s.group(1)
    if not seller and re.search(r"ships from\s+and\s+sold by\s+amazon", low):
        seller = "Amazon"
    return (seller or "Unknown").strip()


def amazon_variations(html):
    """Child ASIN -> human label (e.g. '4U') for parent listings."""
    m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{[^{}]*\})', html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return {a: " / ".join(str(v) for v in vals)
                for a, vals in data.items()}
    except (ValueError, AttributeError):
        return {a: "variation"
                for a in re.findall(r'"(B[A-Z0-9]{9})"\s*:', m.group(1))}


def check_amazon(asin):
    out = {"price": None, "seller": "", "buybox": "No", "status": ""}
    try:
        r = http_get("https://www.amazon.in/dp/" + asin)
    except requests.RequestException:
        out["status"] = "UNVERIFIED (NETWORK)"
        return out
    low = r.text.lower()
    if r.status_code in (429, 503) or "captcha" in low:
        out["status"] = "UNVERIFIED (BLOCKED)"
        return out
    if r.status_code == 404:
        out["status"] = "NOT FOUND"
        return out
    if r.status_code != 200:
        out["status"] = "UNVERIFIED (HTTP %s)" % r.status_code
        return out
    html = r.text
    price = first_match(html, [
        r'"priceToPay"[^}]*?"value"\s*:\s*([\d.]+)',
        r'"apexPriceToPay"[^\]]*?"value"\s*:\s*([\d.]+)',
        r'class="a-price-whole">([\d,]+)',
    ])
    out["price"] = to_number(price)
    if out["price"] is not None:
        out["seller"] = amazon_seller(html, low)
        out["buybox"] = "Yes"
        out["status"] = "OK"
        return out
    children = amazon_variations(html)
    if children:
        out["children"] = children
        out["status"] = "PARENT"
        return out
    if "currently unavailable" in low:
        out["status"] = "UNAVAILABLE"
    else:
        out["status"] = "UNVERIFIED (NO PRICE)"
    return out


# -------------------------------------------------------------- flipkart
def flipkart_seller(html):
    # new layout: seller name lives in the ATLAS seller-details widget
    i = html.find("seller_details_seller_title")
    if i != -1:
        m = re.search(r'"label_0".{0,200}?"text"\s*:\s*"([^"]+)"',
                      html[i:i + 4000], re.S)
        if m:
            return m.group(1).strip()
    return (first_match(html, [
        r'"sellerName"\s*:\s*"([^"]+)"',
        r'id="sellerName"[^>]*>.{0,120}?>([^<]+)<',
    ], flags=re.S) or "Unknown").strip()


def check_flipkart(fsn):
    out = {"price": None, "seller": "", "buybox": "No", "status": ""}
    try:
        r = http_get("https://www.flipkart.com/product/p/itm?pid=" + fsn)
    except requests.RequestException:
        out["status"] = "UNVERIFIED (NETWORK)"
        return out
    low = r.text.lower()
    if r.status_code in (403, 429, 503) or "are you a human" in low:
        out["status"] = "UNVERIFIED (BLOCKED)"
        return out
    if r.status_code == 404 or "product not found" in low:
        out["status"] = "NOT FOUND"
        return out
    if r.status_code != 200:
        out["status"] = "UNVERIFIED (HTTP %s)" % r.status_code
        return out
    if "currently out of stock" in low or "sold out" in low:
        out["status"] = "UNAVAILABLE"
        return out
    html = r.text
    price = first_match(html, [
        r'"finalPrice"\s*:\s*\{\s*"[^"]*"\s*:\s*[^,}]*,?\s*"value"\s*:\s*(\d+)',
        r'"finalPrice"\s*:\s*\{\s*"value"\s*:\s*(\d+)',
        r'₹([\d,]+)\s*<',
    ])
    out["price"] = to_number(price)
    out["seller"] = flipkart_seller(html)
    if out["price"] is not None:
        out["buybox"] = "Yes"
        out["status"] = "OK"
    else:
        out["status"] = "UNVERIFIED (NO PRICE)"
    return out


# ----------------------------------------------------------------- sheet
def open_sheet():
    sheet_id = os.environ.get("SHEET_ID", "").strip()
    if not sheet_id:
        sys.exit("SHEET_ID env var is not set")
    key_file = os.environ.get("SERVICE_ACCOUNT_FILE", "").strip()
    raw = os.environ.get("GCP_SERVICE_ACCOUNT", "").strip()
    if key_file:
        creds = Credentials.from_service_account_file(key_file, scopes=SCOPES)
    elif raw:
        creds = Credentials.from_service_account_info(json.loads(raw),
                                                      scopes=SCOPES)
    else:
        sys.exit("Set GCP_SERVICE_ACCOUNT or SERVICE_ACCOUNT_FILE")
    return gspread.authorize(creds).open_by_key(sheet_id)


def ws(sh, name, headers):
    try:
        w = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        w = sh.add_worksheet(name, rows=200, cols=len(headers) + 2)
        w.update([headers])
    return w


def records(w):
    rows = w.get_all_values()
    if not rows:
        return []
    head = rows[0]
    return [dict(zip(head, r)) for r in rows[1:] if any(c.strip() for c in r)]


# ------------------------------------------------------------------ main
def main():
    sh = open_sheet()
    products = records(ws(sh, "products", PRODUCT_COLS))
    run_time = datetime.now(IST).strftime("%d %b %Y, %H:%M IST")

    seen_ids = set()
    rows = []

    def record(brand, sku, name, msp, market, pid, res):
        below = "YES" if (res["price"] is not None
                          and res["price"] < msp) else "NO"
        rows.append([run_time, brand, sku, name, market, pid, msp,
                     res["seller"],
                     res["price"] if res["price"] is not None else "",
                     res["buybox"], below, res["status"]])
        print(f"  {market} {pid}: price={res['price']} "
              f"seller={res['seller']!r} status={res['status']} "
              f"below_msp={below}")

    for p in products:
        brand = p.get("Brand", "").strip()
        sku = p.get("SKU", "").strip()
        name = p.get("Product Name", "").strip()
        msp = to_number(p.get("MSP"))
        if not name or msp is None:
            continue

        asin = p.get("ASIN", "").strip().upper()
        if asin and ("Amazon", asin) not in seen_ids:
            seen_ids.add(("Amazon", asin))
            res = with_retry(check_amazon, asin)
            if res.get("children"):
                kids = list(res["children"].items())[:MAX_VARIATIONS]
                print(f"  Amazon {asin}: parent listing, "
                      f"checking {len(kids)} variation(s)")
                for kid, label in kids:
                    if ("Amazon", kid) in seen_ids:
                        continue
                    seen_ids.add(("Amazon", kid))
                    polite_pause()
                    kres = with_retry(check_amazon, kid)
                    if kres.get("children"):  # never recurse twice
                        kres = {"price": None, "seller": "", "buybox": "No",
                                "status": "UNVERIFIED (NO PRICE)"}
                    record(brand, sku, f"{name} ({label})", msp,
                           "Amazon", kid, kres)
            else:
                record(brand, sku, name, msp, "Amazon", asin, res)
            polite_pause()

        fsn = p.get("FSN", "").strip().upper()
        if fsn and ("Flipkart", fsn) not in seen_ids:
            seen_ids.add(("Flipkart", fsn))
            record(brand, sku, name, msp, "Flipkart", fsn,
                   with_retry(check_flipkart, fsn))
            polite_pause()

    w_res = ws(sh, "results", RESULT_COLS)
    w_res.clear()
    w_res.update([RESULT_COLS] + [[str(c) for c in r] for r in rows])

    # ------------------------------------------------------------- tracker
    w_trk = ws(sh, "tracker", TRACKER_COLS)
    tracker = {(t.get("Brand", ""), t.get("Product", ""),
                t.get("Marketplace", "")): t for t in records(w_trk)}
    for r in rows:
        key = (r[1], r[3], r[4])
        price, below, status = to_number(r[8]), r[10], r[11]
        entry = tracker.get(key)
        if below == "YES":
            if entry and entry.get("Status", "").lower() == "open":
                sellers = {s.strip() for s in
                           entry.get("Sellers", "").split(",") if s.strip()}
                sellers.add(r[7])
                lowest = min(x for x in
                             [to_number(entry.get("Lowest Seen")), price]
                             if x is not None)
                entry.update({"Sellers": ", ".join(sorted(sellers)),
                              "Lowest Seen": lowest,
                              "Runs Seen":
                                  int(to_number(entry.get("Runs Seen")) or 0)
                                  + 1})
            else:
                tracker[key] = {"Brand": r[1], "Product": r[3],
                                "Marketplace": r[4], "Sellers": r[7],
                                "Lowest Seen": price, "MSP": r[6],
                                "First Seen": run_time, "Runs Seen": 1,
                                "Status": "open"}
        elif status == "OK" and entry \
                and entry.get("Status", "").lower() == "open":
            entry["Status"] = "closed"
    w_trk.clear()
    w_trk.update([TRACKER_COLS] +
                 [[str(t.get(c, "")) for c in TRACKER_COLS]
                  for t in tracker.values()])

    vio = sum(1 for r in rows if r[10] == "YES")
    unv = sum(1 for r in rows if str(r[11]).startswith("UNVERIFIED"))
    print(f"Run complete: {len(rows)} listings checked, "
          f"{vio} below MSP, {unv} unverified.")


if __name__ == "__main__":
    main()
