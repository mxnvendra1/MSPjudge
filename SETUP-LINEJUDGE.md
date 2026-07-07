# LineJudge — Setup Guide (one time, ~30 minutes)

End state: your team opens **linejudge.streamlit.app** (or similar), sees
violations for Hundred and Li-Ning, and adds/edits products right there.
Products are stored centrally in a Google Sheet — added once, kept forever
until deleted. Your Mac still runs the price checks and syncs both ways.

There are 3 phases. Do them in order.

---

## Phase 1 — The Google Sheet (the central store)

1. Go to sheets.google.com → create a blank spreadsheet → name it
   "LineJudge Data".
2. Copy the **sheet ID** from the URL — the long code between /d/ and /edit:
   docs.google.com/spreadsheets/d/ **THIS-LONG-CODE** /edit
   Keep it handy; you'll paste it in two places.
   (Don't worry about tabs/columns — they get created automatically.)

## Phase 2 — The service account (lets the app & checker write to the sheet)

This is the fiddliest part. It's all clicking, no code.

1. Go to console.cloud.google.com (sign in with the same Google account).
2. Top bar → project dropdown → **New Project** → name it "linejudge" → Create.
3. Make sure the new project is selected, then in the search bar type
   **Google Sheets API** → open it → **Enable**. Do the same for
   **Google Drive API**.
4. Menu (☰) → IAM & Admin → **Service Accounts** → Create service account.
   Name: "linejudge" → Create and continue → skip the optional steps → Done.
5. Click the service account you just made → **Keys** tab →
   Add key → Create new key → **JSON** → Create.
   A .json file downloads. This file is a password — don't share it.
6. Open that .json file in a text editor. Find the **client_email** line
   (looks like linejudge@linejudge-xxxx.iam.gserviceaccount.com) and copy it.
7. Back in your Google Sheet → **Share** → paste that email → role
   **Editor** → Share. (This is how the robot gets access to your sheet.)

## Phase 3a — Deploy the app to Streamlit Cloud

1. Push the `app` folder to a **new GitHub repo** (this one can be PRIVATE —
   Streamlit Cloud reads private repos fine, so your logos and data setup
   aren't public). Easiest way: github.com → New repository → "linejudge" →
   Private → create, then "uploading an existing file" link → drag in
   everything inside the `app` folder (streamlit_app.py, requirements.txt,
   the assets folder, the .streamlit folder) → Commit.
2. Go to share.streamlit.io → sign in with GitHub → **New app** →
   pick the linejudge repo → main file: `streamlit_app.py` → Deploy.
3. While it builds: app menu (⋮) → **Settings → Secrets** → paste the
   contents of `.streamlit/secrets.example.toml`, then fill in:
   - `app_password` — the shared password your team will type
   - `sheet_id` — from Phase 1
   - the `[gcp_service_account]` block — copy each field from the downloaded
     .json file (project_id, private_key, client_email, etc. — the names
     match one-to-one).
   Save. The app restarts and is live at your link.

## Phase 3b — Point the Mac checker at the sheet

1. Copy the downloaded .json key into the msp-checker folder and rename it
   **service_account.json**.
2. Install the sheet library (one time), in Terminal:
       python3 -m pip install gspread --break-system-packages
3. In config.json set:
       "google_sheet": { "enabled": true, "sheet_id": "THE-SAME-ID", ... }
4. Run the checker as usual (run.command). You'll see:
   "Loaded N products from the Google Sheet" at the start and
   "Results pushed to the Google Sheet" at the end. Refresh LineJudge —
   the run is on screen.

products.csv is now only a fallback — the sheet is the master list.

---

## Daily life after setup

- Team adds/edits products at the LineJudge link (single add, bulk paste
  from Excel, or edit rows inline and hit Save). Stored until deleted.
- You (or a scheduled job) run the checker on the Mac once or twice a day.
- Everyone sees violations at the link, filtered by brand, phone-friendly.
- Email alerts and the GitHub Pages report still work if enabled — LineJudge
  doesn't replace them, it sits on top of the same data.

## If something breaks

- App says it can't reach the sheet → the service-account email probably
  isn't shared as Editor on the sheet (Phase 2, step 7).
- Checker says "Sheet push failed" → same cause, or wrong sheet_id.
- Wrong password loop → check app_password in Streamlit Secrets.
- Anything else → screenshot/paste the error to Claude.
