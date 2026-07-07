# Hundred MSP Price Checker — Setup (one time, ~10 minutes)

After this setup, running a check is just double-clicking one file.

## 1. Install Python (one time)
- **Windows:** go to python.org → Downloads → install. IMPORTANT: on the
  first installer screen, tick the checkbox **"Add Python to PATH"**.
- **Mac:** Python 3 is usually already installed. If not, install from python.org.

## 2. Install the two libraries (one time)
Open Command Prompt (Windows) or Terminal (Mac) and run these two lines:

    pip install playwright
    playwright install chromium

The second line downloads the browser the checker uses. Done.

## 3. Put your products in
Replace the sample row in `products.csv` with your real list.
Easiest way: in the dashboard, click **Export CSV**, then rename that file
to `products.csv` and drop it in this folder (replace the old one).

Columns: SKU, Product Name, ASIN, FSN, MSP. A product can have only an
ASIN or only an FSN — leave the other blank.

## 4. Run it
- **Windows:** double-click `run.bat`
- **Mac:** double-click `run.command`
  (first time, right-click → Open, because it's from outside the App Store)

A window shows live progress. For 100 products on both marketplaces
(~200 pages), a run takes roughly 40–50 minutes — it deliberately goes
slow with human-like pauses so Amazon/Flipkart don't block it. Let it
run in the background.

When done, the report opens in your browser automatically. Everything is
also saved in the `reports` folder (an HTML report + a results CSV per run).

## 5. Email alerts (optional)
Open `config.json` in Notepad, set `"enabled": true` under email, and fill
in your SMTP details. For Gmail: use an **App Password**
(Google Account → Security → 2-Step Verification → App passwords),
not your normal password. Add your team's addresses under `"to"`.

If email stays disabled, nothing breaks — the report just saves locally.

## What the report shows
- **Violations** — every seller below MSP, by how much (₹ and %), with the
  Buy Box holder marked, and a link to the listing.
- **Could not verify** — pages the checker couldn't read this run
  (captcha, layout change). These are NOT assumed compliant; retried next run.
- **Open violation tracker** — every violation stays listed run after run
  until the price returns to MSP, then it auto-resolves. Nobody has to
  remember to re-check.

## Things to know (honest notes)
- Run it once or twice a day maximum. More than that risks blocks.
- Amazon/Flipkart change their page layout a few times a year. When that
  happens, some products show "could not read price". That's a small script
  fix — bring the error message back to Claude and it can be patched.
- Run it from your normal office/home internet, not a VPN or datacenter —
  a normal connection is what makes it look like a human browsing.
