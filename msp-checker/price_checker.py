"""
Hundred - MSP Price Checker (v1)
================================
Reads products.csv (SKU, Product Name, ASIN, FSN, MSP), visits each
Amazon.in and Flipkart listing, collects every seller's price it can see,
compares against MSP, tracks violations until resolved, writes an HTML
report + results CSV, and (optionally) emails the report.

Run it with:  python price_checker.py
Or double-click run.bat (Windows) / run.command (Mac).
"""

import csv
import json
import os
import random
import re
import smtplib
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------- paths
BASE = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_CSV = os.path.join(BASE, "products.csv")
CONFIG_JSON = os.path.join(BASE, "config.json")
TRACKER_JSON = os.path.join(BASE, "violations_tracker.json")
REPORTS_DIR = os.path.join(BASE, "reports")

MIN_DELAY, MAX_DELAY = 8, 15  # polite gap between pages, seconds


# ---------------------------------------------------------------- helpers
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_price(text):
    """Pull a rupee amount out of messy text like '₹1,299.00'."""
    if not text:
        return None
    m = re.search(r"([\d,]+(?:\.\d{1,2})?)", text.replace("\u20b9", "").strip())
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def flipkart_jsonld(content):
    """Flipkart embeds structured Product data (JSON-LD) for search engines.
    Most reliable source for the true selling price + seller name."""
    price, seller = None, None
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        content or "", re.S,
    ):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for d in data if isinstance(data, list) else [data]:
            if not isinstance(d, dict) or d.get("@type") != "Product":
                continue
            offers = d.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if isinstance(offers, dict):
                if offers.get("price") is not None:
                    price = parse_price(str(offers["price"]))
                s = offers.get("seller")
                if isinstance(s, dict) and s.get("name"):
                    seller = str(s["name"]).strip()
            if price:
                return price, seller
    return price, seller


def seller_from_html(html):
    """Fallback: find 'Sold by <Name>' / 'Fulfilled by <Name>' in raw HTML,
    regardless of what CSS classes the site is using this month."""
    if not html:
        return None
    m = re.search(
        r"(?:Sold|Fulfilled)\s+by\s*:?\s*(?:</?[^>]{0,120}>\s*)*"
        r"([A-Za-z0-9][A-Za-z0-9&.'()\-_, ]{1,60})",
        html,
    )
    if not m:
        return None
    name = m.group(1)
    # cut off ratings/tenure junk like '4.1★ • 5 years with Flipkart'
    name = re.split(r"\s*\d\.\d|\u2605|\u2022|\s{2,}|\byears?\b", name)[0]
    name = name.strip(" .,:;|-")
    junk = {"amazon", "flipkart", "seller", ""}
    return None if name.lower() in junk else name


def inr(n):
    if n is None:
        return "-"
    s = f"{n:,.0f}" if float(n) == int(n) else f"{n:,.2f}"
    return "\u20b9" + s


def load_config():
    if os.path.exists(CONFIG_JSON):
        with open(CONFIG_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_products():
    if not os.path.exists(PRODUCTS_CSV):
        log("ERROR: products.csv not found next to this script.")
        log("Export it from the dashboard (Export CSV) and place it here.")
        sys.exit(1)
    products = []
    with open(PRODUCTS_CSV, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            # tolerate header variations from the dashboard export
            get = lambda *names: next(
                (row[n].strip() for n in names if n in row and row[n]), ""
            )
            p = {
                "brand": get("Brand", "brand"),
                "sku": get("SKU", "sku"),
                "name": get("Product Name", "name", "Product"),
                "asin": get("ASIN", "asin").upper(),
                "fsn": get("FSN", "fsn").upper(),
                "msp": parse_price(get("MSP", "msp")),
            }
            if p["name"] and p["msp"] and (p["asin"] or p["fsn"]):
                products.append(p)
            else:
                log(f"Skipping incomplete row: {row}")
    return products


# ---------------------------------------------------------------- amazon
def extract_variations(content):
    """Pull child ASIN -> variation label from the twister JSON Amazon
    embeds in the page source."""
    m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{.*?\})\s*,', content, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return {asin: " / ".join(vals) for asin, vals in data.items()}
    except Exception:
        return {}


def check_amazon(page, asin, allow_expand=True):
    """
    Returns (offers, error, children).
    - offers: list of {seller, price, buybox}
    - children: dict of child ASIN -> variation label when this is a parent
      listing with no selected variation (in that case offers is empty).
    Uses the All Offers Display (AOD) panel so we see every seller.
    """
    offers = []
    url = f"https://www.amazon.in/dp/{asin}?aod=1"
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)

    title = (page.title() or "").lower()
    if "robot" in title or page.locator(
        "form[action*='validateCaptcha']"
    ).count():
        return [], "captcha", None
    content = page.content() or ""
    if "page not found" in title or "dogs of amazon" in content.lower():
        return [], "listing not found", None

    # --- parent listing with variations and nothing selected?
    # Amazon shows "To buy, select <Size/Colour>" and a price RANGE, with
    # no seller and no real buy box. Detect it and hand back the child ASINs.
    if allow_expand:
        looks_parent = bool(re.search(r"to buy,\s*select", content, re.I)) or (
            page.locator("#twister").count() > 0
            and page.locator("#add-to-cart-button").count() == 0
            and page.locator("#aod-offer").count() == 0
        )
        if looks_parent:
            children = extract_variations(content)
            if children:
                return [], None, children
            return [], ("variation listing with nothing selected - could not "
                        "list variations; add the specific variation ASINs "
                        "to products.csv"), None

    # --- pinned offer (buy box winner) inside AOD panel
    try:
        pinned = page.locator("#aod-pinned-offer")
        if pinned.count():
            price_el = pinned.locator(".a-price .a-offscreen").first
            price = parse_price(price_el.inner_text()) if price_el.count() else None
            seller = None
            sold_by = pinned.locator("#aod-offer-soldBy a, #aod-offer-soldBy .a-color-base")
            if sold_by.count():
                seller = sold_by.first.inner_text().strip()
            if price:
                offers.append({"seller": seller or "Unknown seller",
                               "price": price, "buybox": True})
    except Exception:
        pass

    # --- all other offers in the panel
    try:
        page.wait_for_selector("#aod-offer", timeout=8000)
    except Exception:
        pass
    for i in range(page.locator("#aod-offer").count()):
        try:
            off = page.locator("#aod-offer").nth(i)
            price_el = off.locator(".a-price .a-offscreen").first
            price = parse_price(price_el.inner_text()) if price_el.count() else None
            seller = None
            sold_by = off.locator("#aod-offer-soldBy a, #aod-offer-soldBy .a-color-base")
            if sold_by.count():
                seller = sold_by.first.inner_text().strip()
            if price:
                offers.append({"seller": seller or "Unknown seller",
                               "price": price, "buybox": False})
        except Exception:
            continue

    # --- fallback: plain product page buy box if AOD gave nothing
    if not offers:
        try:
            price_el = page.locator(
                "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, "
                "#apex_desktop .a-price .a-offscreen"
            ).first
            price = parse_price(price_el.inner_text()) if price_el.count() else None
            seller = None
            merch = page.locator(
                "#sellerProfileTriggerId, #merchant-info a, "
                "div[offer-display-feature-name='desktop-merchant-info'] "
                ".offer-display-feature-text-message, "
                "#offerDisplayFeatures .offer-display-feature-text-message, "
                "#merchant-info"
            )
            if merch.count():
                seller = merch.first.inner_text().strip().split("\n")[0]
            if price:
                offers.append({"seller": seller or "Unknown seller",
                               "price": price, "buybox": True})
        except Exception:
            pass

    # last-resort seller fill from visible 'Sold by ...' text anywhere on page
    if offers and any(o["seller"] == "Unknown seller" for o in offers):
        fallback = seller_from_html(page.content())
        if fallback:
            for o in offers:
                if o["seller"] == "Unknown seller":
                    o["seller"] = fallback
                    break

    if not offers:
        return [], "could not read any offer (page layout may have changed)", None

    # dedupe identical seller+price pairs
    seen, unique = set(), []
    for o in offers:
        key = (o["seller"], o["price"])
        if key not in seen:
            seen.add(key)
            unique.append(o)
    return unique, None, None


# ---------------------------------------------------------------- flipkart
def check_flipkart(page, fsn):
    """
    Returns (offers, error). Flipkart prominently shows one seller;
    we reliably read that one, plus others when the page exposes them.
    """
    url = f"https://www.flipkart.com/product/p/itme?pid={fsn}"
    page.goto(url, timeout=60000, wait_until="domcontentloaded")
    page.wait_for_timeout(3500)

    # close the login popup if it appears
    try:
        close_btn = page.locator("button:has-text('✕')")
        if close_btn.count():
            close_btn.first.click()
            page.wait_for_timeout(800)
    except Exception:
        pass

    content = page.content() or ""
    if "we're sorry" in content.lower() and "unable" in content.lower():
        return [], "listing not found"

    # PRIMARY source: the structured Product data (JSON-LD) Flipkart embeds
    # for search engines. Survives CSS class changes and can't grab a price
    # from some unrelated element elsewhere on the page.
    price, ld_seller = flipkart_jsonld(content)

    if price is None:
        # backup: known price classes near the top of the product block
        for sel in ["div.Nx9bqj.CxhGGd", "div._30jeq3._16Jk6d", "div._30jeq3"]:
            el = page.locator(sel)
            if el.count():
                price = parse_price(el.first.inner_text())
                if price:
                    break
    # NOTE: deliberately NO free-text rupee fallback. A wrong price is worse
    # than no price; unreadable pages get reported as "could not verify".

    seller = ld_seller
    for sel in ["#sellerName span span", "#sellerName span", "#sellerName"]:
        el = page.locator(sel)
        if el.count():
            seller = el.first.inner_text().strip().split("\n")[0]
            if seller:
                break
    if not seller:
        # newer layout: 'Fulfilled by GalacticSports' / 'Sold by ...' text block
        try:
            el = page.locator(
                "xpath=//*[starts-with(normalize-space(text()),'Fulfilled by') "
                "or starts-with(normalize-space(text()),'Sold by')]"
            )
            if el.count():
                txt = el.first.inner_text().strip()
                seller = re.sub(r"^(Fulfilled|Sold)\s+by\s*:?\s*", "", txt)
                seller = seller.split("\n")[0].strip()
        except Exception:
            pass
    if not seller:
        seller = seller_from_html(content)
    # strip rating/tenure tails if they got captured
    if seller:
        seller = re.split(r"\s*\d\.\d|\u2605|\u2022", seller)[0].strip(" .,:;|-")

    if price is None:
        return [], "could not read price (page layout may have changed)"
    return [{"seller": seller or "Unknown seller", "price": price,
             "buybox": True}], None


# ---------------------------------------------------------------- tracker
def load_tracker():
    if os.path.exists(TRACKER_JSON):
        with open(TRACKER_JSON, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_tracker(tracker):
    with open(TRACKER_JSON, "w", encoding="utf-8") as f:
        json.dump(tracker, f, indent=2, ensure_ascii=False)


def update_tracker(tracker, results):
    """Open new violations, keep repeat ones, resolve fixed ones."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for r in results:
        key = f"{r['marketplace']}|{r['asin'] or r['fsn']}"
        violating = [o for o in r["offers"] if o["price"] < r["msp"]]
        entry = tracker.get(key)
        if violating:
            worst = min(o["price"] for o in violating)
            if entry and entry.get("status") == "open":
                entry["last_seen"] = now
                entry["times_seen"] = entry.get("times_seen", 1) + 1
                entry["lowest_price"] = min(entry.get("lowest_price", worst), worst)
                entry["sellers"] = sorted(
                    {*(entry.get("sellers") or []),
                     *[o["seller"] for o in violating]}
                )
            else:
                tracker[key] = {
                    "status": "open", "brand": r.get("brand", ""),
                    "sku": r["sku"], "name": r["name"],
                    "marketplace": r["marketplace"], "msp": r["msp"],
                    "first_seen": now, "last_seen": now, "times_seen": 1,
                    "lowest_price": worst,
                    "sellers": sorted({o["seller"] for o in violating}),
                }
        elif entry and entry.get("status") == "open" and not r["error"]:
            entry["status"] = "resolved"
            entry["resolved_on"] = now
    return tracker


# ---------------------------------------------------------------- report
def build_report(results, tracker, run_time):
    violations = [r for r in results
                  if any(o["price"] < r["msp"] for o in r["offers"])]
    errors = [r for r in results if r["error"]]
    ok = [r for r in results if not r["error"] and r not in violations]

    MUTED, INK, RED, AMBER = "#8a8681", "#1c1917", "#c2410c", "#a16207"

    def offer_rows(r):
        rows = ""
        for o in sorted(r["offers"], key=lambda x: x["price"]):
            below = o["price"] < r["msp"]
            diff = r["msp"] - o["price"]
            pct = diff / r["msp"] * 100
            badge = (f'<span style="color:{MUTED};font-size:11px"> · buy box'
                     '</span>') if o.get("buybox") else ""
            if below:
                tail = (f' <span style="color:{RED}">−{inr(diff)}'
                        f" ({pct:.1f}%)</span>")
                price_style = f"color:{RED};font-weight:600"
            else:
                tail = ""
                price_style = f"color:{INK}"
            rows += (f'<div style="font-size:13.5px;margin:3px 0;color:{INK}">'
                     f'{o["seller"]}{badge}'
                     f'<span style="float:right;{price_style}">'
                     f'{inr(o["price"])}{tail}</span></div>'
                     '<div style="clear:both"></div>')
        return rows

    def block(r):
        mp = r["marketplace"]
        pid = r["asin"] if mp == "Amazon" else r["fsn"]
        link = (f"https://www.amazon.in/dp/{r['asin']}" if mp == "Amazon"
                else f"https://www.flipkart.com/product/p/itme?pid={r['fsn']}")
        body = (f'<div style="font-size:13px;color:{AMBER}">{r["error"]}</div>'
                if r["error"] else offer_rows(r))
        return f'''
        <div style="padding:14px 0;border-bottom:1px solid #eceae7">
          <div style="font-size:14px;font-weight:600;color:{INK}">{r["name"]}
            <span style="float:right;font-weight:400;font-size:12px;color:{MUTED}">
              MSP {inr(r["msp"])}</span></div>
          <div style="font-size:11.5px;color:{MUTED};margin:1px 0 8px">
            {(r.get("brand") + " · ") if r.get("brand") else ""}{(r["sku"] + " · ") if r["sku"] else ""}{mp} ·
            <a href="{link}" style="color:{MUTED}">{pid}</a></div>
          {body}
        </div>'''

    def section(label, count, items, empty):
        head = (f'<div style="margin:34px 0 2px;font-size:11px;letter-spacing:'
                f'0.08em;text-transform:uppercase;color:{MUTED}">'
                f"{label} · {count}</div>")
        if not items:
            return head + (f'<div style="padding:12px 0;font-size:13px;'
                           f'color:{MUTED}">{empty}</div>')
        return head + "".join(block(r) for r in items)

    open_v = {k: v for k, v in tracker.items() if v.get("status") == "open"}
    tracker_rows = ""
    for v in open_v.values():
        tracker_rows += (
            f'<tr style="border-bottom:1px solid #eceae7">'
            f'<td style="padding:8px 12px 8px 0">{v["name"]}'
            f'<div style="color:{MUTED};font-size:11px">{v["marketplace"]}'
            f" · since {v['first_seen']} · seen {v['times_seen']} run(s)"
            f"</div></td>"
            f'<td style="padding:8px 12px 8px 0;color:{MUTED}">'
            f'{", ".join(v["sellers"])}</td>'
            f'<td style="padding:8px 0;text-align:right;white-space:nowrap">'
            f'<span style="color:{RED};font-weight:600">'
            f'{inr(v["lowest_price"])}</span>'
            f'<span style="color:{MUTED}"> / {inr(v["msp"])}</span></td></tr>')

    v_color = RED if violations else INK
    html = f'''<!doctype html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Hundred MSP Report</title></head>
    <body style="margin:0;background:#fbfaf9">
    <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',
                Inter,Arial,sans-serif;max-width:680px;margin:0 auto;
                padding:48px 24px 64px;color:{INK}">

      <div style="font-size:19px;font-weight:700;letter-spacing:-0.01em">
        Hundred <span style="font-weight:400;color:{MUTED}">MSP check</span></div>
      <div style="font-size:12px;color:{MUTED};margin-top:2px">{run_time}</div>

      <div style="display:flex;gap:36px;margin:28px 0 4px">
        <div><div style="font-size:26px;font-weight:700;color:{v_color}">
          {len(violations)}</div>
          <div style="font-size:11px;color:{MUTED}">violations</div></div>
        <div><div style="font-size:26px;font-weight:700">{len(ok)}</div>
          <div style="font-size:11px;color:{MUTED}">compliant</div></div>
        <div><div style="font-size:26px;font-weight:700;color:{AMBER if errors else INK}">
          {len(errors)}</div>
          <div style="font-size:11px;color:{MUTED}">unverified</div></div>
        <div><div style="font-size:26px;font-weight:700">{len(results)}</div>
          <div style="font-size:11px;color:{MUTED}">checked</div></div>
      </div>

      {section("Violations", len(violations), violations, "None this run.")}
      {section("Could not verify", len(errors), errors,
               "Everything was readable this run.")}
      {section("Compliant", len(ok), ok, "None.")}

      <div style="margin:34px 0 2px;font-size:11px;letter-spacing:0.08em;
                  text-transform:uppercase;color:{MUTED}">
        Open tracker · {len(open_v)}</div>
      <div style="font-size:12px;color:{MUTED};margin-bottom:6px">
        Each stays listed until its price returns to MSP, then auto-resolves.</div>
      <table style="border-collapse:collapse;width:100%;font-size:13px">
        {tracker_rows or ('<tr><td style="padding:10px 0;color:' + MUTED +
                          '">No open violations.</td></tr>')}
      </table>

      <div style="margin-top:44px;font-size:11px;color:{MUTED}">
        Unverified listings are not assumed compliant; they are retried on the
        next run. Full data in the results CSV alongside this report.</div>
    </div></body></html>'''
    return html, violations, errors


def write_results_csv(results, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["SKU", "Product", "Marketplace", "ID", "MSP",
                    "Seller", "Price", "Buy Box", "Below MSP", "Status"])
        for r in results:
            if r["error"]:
                w.writerow([r["sku"], r["name"], r["marketplace"],
                            r["asin"] or r["fsn"], r["msp"],
                            "", "", "", "", f"UNVERIFIED: {r['error']}"])
            for o in r["offers"]:
                below = o["price"] < r["msp"]
                w.writerow([r["sku"], r["name"], r["marketplace"],
                            r["asin"] or r["fsn"], r["msp"], o["seller"],
                            o["price"], "yes" if o.get("buybox") else "",
                            "YES" if below else "no",
                            "VIOLATION" if below else "ok"])


def send_email(cfg, subject, html):
    e = cfg.get("email", {})
    if not e.get("enabled"):
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = e["from"]
    msg["To"] = ", ".join(e["to"])
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(e["smtp_host"], e.get("smtp_port", 587)) as s:
        s.starttls()
        s.login(e["username"], e["password"])
        s.sendmail(e["from"], e["to"], msg.as_string())
    return True




# ---------------------------------------------------------------- google sheet
def _gs_open(cfg):
    import gspread
    g = cfg["google_sheet"]
    cred_path = os.path.join(BASE, g.get("credentials_file",
                                         "service_account.json"))
    gc = gspread.service_account(filename=cred_path)
    return gc.open_by_key(g["sheet_id"])


def _gs_ws(sh, name, headers):
    try:
        return sh.worksheet(name)
    except Exception:
        w = sh.add_worksheet(name, rows=200, cols=len(headers) + 2)
        w.update([headers])
        return w


def load_products_from_sheet(cfg):
    sh = _gs_open(cfg)
    ws = _gs_ws(sh, "products",
                ["Brand", "SKU", "Product Name", "ASIN", "FSN", "MSP"])
    products = []
    for row in ws.get_all_records():
        p = {
            "brand": str(row.get("Brand", "")).strip(),
            "sku": str(row.get("SKU", "")).strip(),
            "name": str(row.get("Product Name", "")).strip(),
            "asin": str(row.get("ASIN", "")).strip().upper(),
            "fsn": str(row.get("FSN", "")).strip().upper(),
            "msp": parse_price(str(row.get("MSP", ""))),
        }
        if p["name"] and p["msp"] and (p["asin"] or p["fsn"]):
            products.append(p)
    return products


def push_results_to_sheet(cfg, results, tracker, run_time):
    sh = _gs_open(cfg)
    rows = [["Run Time", "Brand", "SKU", "Product", "Marketplace", "ID",
             "MSP", "Seller", "Price", "Buy Box", "Below MSP", "Status"]]
    for r in results:
        base = [run_time, r.get("brand", ""), r["sku"], r["name"],
                r["marketplace"], r["asin"] or r["fsn"], r["msp"]]
        if r["error"]:
            rows.append(base + ["", "", "", "",
                                f"UNVERIFIED: {r['error']}"])
        for o in r["offers"]:
            below = o["price"] < r["msp"]
            rows.append(base + [o["seller"], o["price"],
                                "yes" if o.get("buybox") else "",
                                "YES" if below else "no",
                                "VIOLATION" if below else "ok"])
    ws = _gs_ws(sh, "results", rows[0])
    ws.clear()
    ws.update(rows)

    trows = [["Brand", "Product", "Marketplace", "Sellers", "Lowest Seen",
              "MSP", "First Seen", "Runs Seen", "Status"]]
    for v in tracker.values():
        trows.append([v.get("brand", ""), v["name"], v["marketplace"],
                      ", ".join(v["sellers"]), v["lowest_price"], v["msp"],
                      v["first_seen"], v.get("times_seen", 1),
                      v.get("status", "open")])
    ws2 = _gs_ws(sh, "tracker", trows[0])
    ws2.clear()
    ws2.update(trows)


# ---------------------------------------------------------------- publish
def publish_report(cfg, html):
    """Push the latest report to a GitHub repo serving GitHub Pages.
    Only the site folder (index.html) is a git repo - config.json,
    products.csv and everything else NEVER leave this machine."""
    pub = cfg.get("publish", {})
    if not pub.get("enabled"):
        return False
    repo_url = (pub.get("repo_url") or "").strip()
    if not repo_url:
        log("Publish is enabled but repo_url is empty in config.json")
        return False

    site = os.path.join(BASE, pub.get("folder", "site"))
    os.makedirs(site, exist_ok=True)
    with open(os.path.join(site, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    def git(*args):
        return subprocess.run(["git", "-C", site, *args],
                              capture_output=True, text=True)

    if not os.path.exists(os.path.join(site, ".git")):
        git("init", "-b", "main")
        git("remote", "add", "origin", repo_url)
    git("remote", "set-url", "origin", repo_url)
    git("config", "user.name", "MSP Checker")
    git("config", "user.email", "msp-checker@local")
    git("add", "index.html")
    git("commit", "-m", f"MSP report {datetime.now():%Y-%m-%d %H:%M}")
    r = git("push", "-u", "origin", "main", "--force")
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()
        log("Publish failed: " + (tail[-1] if tail else "unknown git error"))
        log("(report is still saved locally in the reports folder)")
        return False
    return True


# ---------------------------------------------------------------- main
def main():
    cfg = load_config()
    if cfg.get("google_sheet", {}).get("enabled"):
        try:
            products = load_products_from_sheet(cfg)
            log(f"Loaded {len(products)} products from the Google Sheet")
        except Exception as ex:
            log(f"Google Sheet read failed ({ex}); falling back to products.csv")
            products = load_products()
    else:
        products = load_products()
        log(f"Loaded {len(products)} products from products.csv")
    if not products:
        sys.exit(1)

    tracker = load_tracker()
    results = []
    run_time = datetime.now().strftime("%d %b %Y, %I:%M %p")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=cfg.get("headless", True))
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 768},
            locale="en-IN",
        )
        page = ctx.new_page()

        jobs = []
        for p in products:
            if p["asin"]:
                jobs.append((p, "Amazon"))
            if p["fsn"]:
                jobs.append((p, "Flipkart"))
        log(f"{len(jobs)} listing checks to run "
            f"(~{len(jobs) * 12 // 60} min at polite speed)")

        known_asins = {p["asin"] for p in products if p["asin"]}
        max_var = int(cfg.get("max_variations", 10))

        for n, (p, mp) in enumerate(jobs, 1):
            label = f"[{n}/{len(jobs)}] {mp} · {p['name']}"
            children = None
            try:
                if mp == "Amazon":
                    offers, err, children = check_amazon(page, p["asin"])
                else:
                    offers, err = check_flipkart(page, p["fsn"])
            except Exception as ex:
                offers, err = [], f"unexpected error: {ex.__class__.__name__}"

            if children:
                # Parent listing: check each variation as its own listing,
                # against this row's MSP. Skip children already in the CSV.
                todo = [(a, v) for a, v in children.items()
                        if a not in known_asins][:max_var]
                skipped = len(children) - len(todo)
                log(f"{label} -> variation listing with {len(children)} "
                    f"variants; checking {len(todo)}"
                    + (f" ({skipped} already in your CSV or over the "
                       f"max_variations limit)" if skipped else ""))
                for c_asin, c_label in todo:
                    time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
                    try:
                        c_offers, c_err, _ = check_amazon(
                            page, c_asin, allow_expand=False)
                    except Exception as ex:
                        c_offers, c_err = [], (
                            f"unexpected error: {ex.__class__.__name__}")
                    child = {**p, "asin": c_asin,
                             "name": f"{p['name']} [{c_label}]",
                             "marketplace": mp, "offers": c_offers,
                             "error": c_err}
                    results.append(child)
                    if c_err:
                        log(f"    · {c_label} -> UNVERIFIED ({c_err})")
                    else:
                        worst = min(o["price"] for o in c_offers)
                        flag = "VIOLATION" if worst < p["msp"] else "ok"
                        log(f"    · {c_label} -> {len(c_offers)} offer(s), "
                            f"lowest {inr(worst)} vs MSP {inr(p['msp'])} [{flag}]")
            else:
                results.append({**p, "marketplace": mp, "offers": offers,
                                "error": err})
                if err:
                    log(f"{label} -> UNVERIFIED ({err})")
                else:
                    worst = min(o["price"] for o in offers)
                    flag = "VIOLATION" if worst < p["msp"] else "ok"
                    log(f"{label} -> {len(offers)} offer(s), lowest {inr(worst)} "
                        f"vs MSP {inr(p['msp'])} [{flag}]")
            if n < len(jobs):
                time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        browser.close()

    tracker = update_tracker(tracker, results)
    save_tracker(tracker)

    if cfg.get("google_sheet", {}).get("enabled"):
        try:
            push_results_to_sheet(cfg, results, tracker, run_time)
            log("Results pushed to the Google Sheet (LineJudge is updated).")
        except Exception as ex:
            log(f"Sheet push failed ({ex}); results still saved locally.")

    os.makedirs(REPORTS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    html, violations, errors = build_report(results, tracker, run_time)
    report_path = os.path.join(REPORTS_DIR, f"report_{stamp}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    write_results_csv(results, os.path.join(REPORTS_DIR, f"results_{stamp}.csv"))

    log(f"Report saved: {report_path}")
    log(f"{len(violations)} violation(s), {len(errors)} unverified.")

    subject = (f"MSP ALERT: {len(violations)} violation(s) - Hundred"
               if violations else "MSP check: all clear - Hundred")
    try:
        if send_email(cfg, subject, html):
            log("Email sent.")
        else:
            log("Email disabled in config.json (report saved locally).")
    except Exception as ex:
        log(f"Email failed ({ex}); report is still saved locally.")

    try:
        if publish_report(cfg, html):
            log("Published - the team link now shows this run's report.")
    except Exception as ex:
        log(f"Publish failed ({ex}); report is still saved locally.")

    if cfg.get("open_report", True):
        try:
            webbrowser.open("file://" + report_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
