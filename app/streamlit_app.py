"""
MSP Judge — every price, in or out.
MSP monitoring console for Hundred & Li-Ning.
Data lives in a Google Sheet; the price checker on a local machine
reads products from it and writes results back.
"""

import re
from datetime import datetime
from pathlib import Path

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

# ----------------------------------------------------------------- config
st.set_page_config(
    page_title="MSP Judge",
    page_icon="🏸",
    layout="centered",
    initial_sidebar_state="collapsed",
)

ASSETS = Path(__file__).parent / "assets"
BRANDS = {
    "Hundred": str(ASSETS / "hundred.png"),
    "Li-Ning": str(ASSETS / "lining.png"),
}
INK, MUTED, RED, AMBER = "#1c1917", "#8a8681", "#c2410c", "#a16207"

st.markdown(
    f"""<style>
    #MainMenu, footer, header {{visibility: hidden;}}
    .block-container {{padding-top: 2.2rem; padding-bottom: 3rem; max-width: 760px;}}
    .stApp {{background: #fbfaf9;}}
    h1, h2, h3, p, span, div {{color: {INK};}}
    .lj-muted {{color: {MUTED}; font-size: 12px;}}
    .lj-section {{margin: 26px 0 2px; font-size: 11px; letter-spacing: .08em;
                  text-transform: uppercase; color: {MUTED};}}
    .lj-card {{padding: 12px 0; border-bottom: 1px solid #eceae7;}}
    div[data-testid="stMetricValue"] {{font-size: 26px;}}
    button[kind="primary"] {{background: {INK}; border: none;}}
    </style>""",
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------- gate
def gate():
    pw = st.secrets.get("app_password", "")
    if not pw or st.session_state.get("authed"):
        return True
    st.markdown("### MSP Judge")
    entered = st.text_input("Team password", type="password")
    if entered and entered == pw:
        st.session_state.authed = True
        st.rerun()
    elif entered:
        st.error("Wrong password.")
    return False


if not gate():
    st.stop()

# ----------------------------------------------------------------- sheets
missing = [k for k in ("gcp_service_account", "sheet_id")
           if k not in st.secrets]
if missing:
    st.warning(
        f"Google Sheets isn't connected yet — missing secret(s): "
        f"**{', '.join(missing)}**.\n\n"
        "On Streamlit Cloud: open **Manage app → ⋮ → Settings → Secrets** "
        "and paste your `sheet_id` plus the `[gcp_service_account]` block "
        "from your Google service-account JSON. The app reloads on save."
    )
    st.stop()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]

PRODUCT_COLS = ["Brand", "SKU", "Product Name", "ASIN", "FSN", "MSP"]


@st.cache_resource
def sheet():
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES
    )
    gc = gspread.authorize(creds)
    return gc.open_by_key(st.secrets["sheet_id"])


def ws(name, headers):
    sh = sheet()
    try:
        w = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        w = sh.add_worksheet(name, rows=200, cols=len(headers) + 2)
        w.update([headers])
    return w


@st.cache_data(ttl=30)
def read_df(name, headers):
    records = ws(name, headers).get_all_records()
    df = pd.DataFrame(records)
    for h in headers:
        if h not in df.columns:
            df[h] = ""
    return df[headers] if len(df) else pd.DataFrame(columns=headers)


def write_products(df):
    w = ws("products", PRODUCT_COLS)
    w.clear()
    rows = [PRODUCT_COLS] + df.fillna("").astype(str).values.tolist()
    w.update(rows)
    read_df.clear()


# ----------------------------------------------------------------- helpers
def clean(s):
    return (s or "").strip().upper()


def valid_asin(s):
    return bool(re.fullmatch(r"[A-Z0-9]{10}", s))


def valid_fsn(s):
    return bool(re.fullmatch(r"[A-Z0-9]{13,20}", s))


def inr(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "-"
    s = f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"
    return "₹" + s


# ----------------------------------------------------------------- header
c1, c2, c3 = st.columns([5, 1.1, 1.1], vertical_alignment="center")
with c1:
    st.markdown(
        f"<div style='font-size:22px;font-weight:700;letter-spacing:-0.01em'>"
        f"MSP Judge <span style='font-weight:400;color:{MUTED};font-size:14px'>"
        f"every price, in or out</span></div>",
        unsafe_allow_html=True,
    )
with c2:
    st.image(BRANDS["Hundred"], use_container_width=True)
with c3:
    st.image(BRANDS["Li-Ning"], use_container_width=True)

brand_filter = st.segmented_control(
    "Brand", options=["All"] + list(BRANDS), default="All",
    label_visibility="collapsed",
)
brand_filter = brand_filter or "All"

tab_results, tab_products = st.tabs(["Results", "Products"])

# ----------------------------------------------------------------- results
RESULT_COLS = ["Run Time", "Brand", "SKU", "Product", "Marketplace", "ID",
               "MSP", "Seller", "Price", "Buy Box", "Below MSP", "Status"]
TRACKER_COLS = ["Brand", "Product", "Marketplace", "Sellers", "Lowest Seen",
                "MSP", "First Seen", "Runs Seen", "Status"]

with tab_results:
    res = read_df("results", RESULT_COLS)
    if brand_filter != "All" and len(res):
        res = res[res["Brand"].str.strip().str.lower()
                  == brand_filter.lower()]

    if not len(res):
        st.markdown(
            f"<div class='lj-muted' style='padding:40px 0;text-align:center'>"
            "No results yet. Once the price checker completes a run, "
            "everything shows up here.</div>",
            unsafe_allow_html=True,
        )
    else:
        run_time = res["Run Time"].iloc[0] if "Run Time" in res else ""
        vio = res[res["Below MSP"].astype(str).str.upper() == "YES"]
        unv = res[res["Status"].astype(str).str.startswith("UNVERIFIED")]
        listings = res.groupby(["Marketplace", "ID"]).ngroups
        ok_count = listings - vio.groupby(["Marketplace", "ID"]).ngroups \
            - unv.groupby(["Marketplace", "ID"]).ngroups

        st.markdown(f"<div class='lj-muted'>last run · {run_time}</div>",
                    unsafe_allow_html=True)
        m1, m2, m3 = st.columns(3)
        m1.metric("violations", int(vio.groupby(["Marketplace", "ID"]).ngroups))
        m2.metric("compliant", max(ok_count, 0))
        m3.metric("unverified", int(unv.groupby(["Marketplace", "ID"]).ngroups))

        st.markdown("<div class='lj-section'>Violations</div>",
                    unsafe_allow_html=True)
        if not len(vio):
            st.markdown("<div class='lj-muted'>None this run.</div>",
                        unsafe_allow_html=True)
        for _, r in vio.iterrows():
            diff = float(r["MSP"]) - float(r["Price"])
            pct = diff / float(r["MSP"]) * 100
            bb = " · buy box" if str(r["Buy Box"]).lower() in ("yes", "true") else ""
            st.markdown(
                f"<div class='lj-card'>"
                f"<div style='font-weight:600;font-size:14px'>{r['Product']}"
                f"<span style='float:right;color:{RED};font-weight:600'>"
                f"{inr(r['Price'])} <span style='font-size:12px'>"
                f"(−{inr(diff)} / {pct:.1f}%)</span></span></div>"
                f"<div class='lj-muted'>{r['Brand']} · {r['Marketplace']}"
                f" · {r['Seller']}{bb} · MSP {inr(r['MSP'])}</div></div>",
                unsafe_allow_html=True,
            )

        trk = read_df("tracker", TRACKER_COLS)
        if len(trk):
            if brand_filter != "All":
                trk = trk[trk["Brand"].str.strip().str.lower()
                          == brand_filter.lower()]
            open_trk = trk[trk["Status"].astype(str).str.lower() == "open"]
            st.markdown(
                f"<div class='lj-section'>Open tracker · {len(open_trk)}</div>",
                unsafe_allow_html=True)
            if len(open_trk):
                st.dataframe(
                    open_trk[["Product", "Marketplace", "Sellers",
                              "Lowest Seen", "MSP", "First Seen", "Runs Seen"]],
                    hide_index=True, use_container_width=True)
            else:
                st.markdown("<div class='lj-muted'>No open violations.</div>",
                            unsafe_allow_html=True)

        with st.expander("Full results table"):
            st.dataframe(res, hide_index=True, use_container_width=True)

# ----------------------------------------------------------------- products
with tab_products:
    prods = read_df("products", PRODUCT_COLS)
    shown = prods if brand_filter == "All" else \
        prods[prods["Brand"].str.strip().str.lower() == brand_filter.lower()]

    st.markdown(
        f"<div class='lj-muted'>{len(prods)} products total · "
        f"{len(shown)} shown · stored centrally, add once</div>",
        unsafe_allow_html=True,
    )

    edited = st.data_editor(
        shown, hide_index=True, use_container_width=True,
        num_rows="dynamic", key=f"editor_{brand_filter}",
        column_config={
            "Brand": st.column_config.SelectboxColumn(
                options=list(BRANDS), required=True),
            "MSP": st.column_config.NumberColumn(min_value=1, format="₹%d"),
        },
    )
    if st.button("Save table changes", type="primary"):
        rest = prods if brand_filter == "All" else \
            prods[prods["Brand"].str.strip().str.lower()
                  != brand_filter.lower()]
        merged = pd.concat([rest, edited], ignore_index=True)
        merged = merged[merged["Product Name"].astype(str).str.strip() != ""]
        merged["ASIN"] = merged["ASIN"].astype(str).map(clean)
        merged["FSN"] = merged["FSN"].astype(str).map(clean)
        write_products(merged)
        st.success("Saved. The next checker run uses this list.")
        st.rerun()

    st.markdown("<div class='lj-section'>Add one product</div>",
                unsafe_allow_html=True)
    with st.form("add", clear_on_submit=True, border=False):
        f1, f2 = st.columns(2)
        brand = f1.selectbox("Brand", list(BRANDS),
                             index=0 if brand_filter in ("All", "Hundred") else 1)
        sku = f2.text_input("SKU code (optional)")
        name = st.text_input("Product name")
        f3, f4, f5 = st.columns(3)
        asin = f3.text_input("Amazon ASIN")
        fsn = f4.text_input("Flipkart FSN")
        msp = f5.number_input("MSP (₹)", min_value=0, step=1)
        if st.form_submit_button("Add product", type="primary"):
            asin, fsn = clean(asin), clean(fsn)
            errs = []
            if not name.strip():
                errs.append("Product name is required.")
            if not asin and not fsn:
                errs.append("Enter at least one of ASIN or FSN.")
            if asin and not valid_asin(asin):
                errs.append("ASIN should be exactly 10 letters/digits.")
            if fsn and not valid_fsn(fsn):
                errs.append("FSN should be 13–20 letters/digits.")
            if msp <= 0:
                errs.append("MSP must be greater than 0.")
            if asin and (prods["ASIN"] == asin).any():
                errs.append("That ASIN is already in the list.")
            if fsn and (prods["FSN"] == fsn).any():
                errs.append("That FSN is already in the list.")
            if errs:
                for e in errs:
                    st.error(e)
            else:
                row = pd.DataFrame([[brand, sku.strip(), name.strip(),
                                     asin, fsn, msp]], columns=PRODUCT_COLS)
                write_products(pd.concat([prods, row], ignore_index=True))
                st.success(f"{name.strip()} added.")
                st.rerun()

    st.markdown("<div class='lj-section'>Bulk paste from Excel</div>",
                unsafe_allow_html=True)
    b_brand = st.selectbox("These rows belong to", list(BRANDS),
                           key="bulk_brand")
    st.markdown(
        "<div class='lj-muted'>Columns in order: SKU · Product name · ASIN · "
        "FSN · MSP — one product per line, leave ASIN or FSN blank if not "
        "on that marketplace.</div>", unsafe_allow_html=True)
    blob = st.text_area("Paste rows", height=140, label_visibility="collapsed")
    if st.button("Check & import rows", type="primary", disabled=not blob.strip()):
        good, bad = [], []
        seen_a = set(prods["ASIN"]) if len(prods) else set()
        seen_f = set(prods["FSN"]) if len(prods) else set()
        for i, line in enumerate([l for l in blob.splitlines() if l.strip()], 1):
            cells = line.split("\t") if "\t" in line else line.split(",")
            cells += [""] * (5 - len(cells))
            sku, name, asin, fsn, msp_raw = [c.strip() for c in cells[:5]]
            asin, fsn = clean(asin), clean(fsn)
            try:
                mspv = float(msp_raw.replace(",", "").replace("₹", ""))
            except ValueError:
                mspv = 0
            problem = None
            if not name:
                problem = "missing product name"
            elif not asin and not fsn:
                problem = "needs an ASIN or FSN"
            elif asin and not valid_asin(asin):
                problem = "ASIN format looks wrong"
            elif fsn and not valid_fsn(fsn):
                problem = "FSN format looks wrong"
            elif mspv <= 0:
                problem = "MSP missing or not a number"
            elif asin and asin in seen_a:
                problem = "duplicate ASIN"
            elif fsn and fsn in seen_f:
                problem = "duplicate FSN"
            if problem:
                bad.append(f"Line {i}: {problem}")
            else:
                seen_a.add(asin)
                seen_f.add(fsn)
                good.append([b_brand, sku, name, asin, fsn, mspv])
        if good:
            add = pd.DataFrame(good, columns=PRODUCT_COLS)
            write_products(pd.concat([prods, add], ignore_index=True))
            st.success(f"{len(good)} product(s) imported to {b_brand}.")
        for b in bad:
            st.warning(b)
        if good:
            st.rerun()
