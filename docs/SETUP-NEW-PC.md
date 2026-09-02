# ACL — Setup on a New Computer

Windows 10/11. About 30 minutes. No coding needed.

**Release to install: `acl-release-2026-09-01`**

---

## Step 0 — Ask Evgeni for these first

You cannot finish without them. They are deliberately not in GitHub.

- [ ] GitHub access to `MojoTheApe/adopy-campaing-launcher`
- [ ] **Your CM name** — exact spelling from the stats workbook
- [ ] **Accounts Google Sheet ID**
- [ ] **`oauth_client.json`** file
- [ ] **Multilogin** login, your profile names, and the automation token
- [ ] The three work files: `cursor example.xlsx`, `Upluad FB.xlsx`, `Creatives` folder

Get the token and the OAuth file through the password manager — never by chat or email.

---

## Step 1 — Install three programs

| Program | Notes |
|---|---|
| **Git for Windows** | Default options are fine |
| **Python 3.11 or newer, 64-bit** | Tick **"Add python.exe to PATH"** during install |
| **Multilogin Desktop** | Sign in, open each of your profiles once, close them, leave the app running |

Check in PowerShell:

```powershell
git --version
python --version
```

If `python` is not recognised, re-run the Python installer with "Add to PATH" ticked and restart Windows.

---

## Step 2 — Download ACL

```powershell
cd $env:USERPROFILE\Documents
git clone https://github.com/MojoTheApe/adopy-campaing-launcher.git ACL
cd ACL
git checkout acl-release-2026-09-01
```

---

## Step 3 — Install it

Double-click **`install_acl.bat`** in the `ACL` folder.

It builds its own Python environment, installs everything, and runs a check.

**The first run will show FAIL lines. That is normal** — your settings are not in place yet. What matters is that it says `Dependencies OK`.

---

## Step 4 — Put your settings in

Open the machine folder:

```powershell
explorer $env:LOCALAPPDATA\ACL
```

**A. Copy `oauth_client.json`** (the file from Evgeni) into that folder.

**B. Open `.env` in Notepad** and set these lines:

```
MY_CM_NAME=Your Name Exactly As In The Workbook
GSHEET_ACCOUNTS_ID=paste-the-sheet-id-here
MULTILOGIN_AUTOMATION_TOKEN_A=paste-the-token-here

FB_PUBLISH_BACKEND=rpa
FB_RPA_ENGINE=recorded_flow

FB_STATS_EXCEL=C:/Users/YOURNAME/Desktop/cursor example.xlsx
UPLOAD_FB_EXCEL=C:/Users/YOURNAME/Desktop/Upluad FB.xlsx
FB_CREATIVES_ROOT=C:/Users/YOURNAME/Desktop/Creatives
```

Three rules for this file:

- Use **forward slashes** in paths, and write the full path — `%USERPROFILE%` does not work here.
- The Sheet ID is only the part between `/d/` and `/edit`, not the whole link.
- Save as `.env`, not `.env.txt`.

---

## Step 5 — Check the machine

```powershell
cd $env:USERPROFILE\Documents\ACL
.\.venv\Scripts\python.exe .\scripts\acl_doctor.py
```

It must end with:

```
All required items present.
```

A `[warn] Telemetry upload` line is fine. A `[FAIL]` line is not — it tells you exactly what is missing and where. Fix that one thing and run the command again.

---

## Step 6 — Start the dashboard

Double-click **`start_dashboard.bat`**.

Keep the black window open — closing it stops ACL. The browser opens `http://127.0.0.1:9090`. The first load takes a minute or two while it reads the workbooks.

Google will ask you to sign in once. Use your company account and approve read-only access to Sheets.

---

## Step 7 — Connect your accounts

In the dashboard, open the **Аккаунты** section and pick your open Multilogin profile for each account. That is all — you do **not** need to edit any spreadsheet.

---

## Rules while setting up

- **Do not publish anything.** Setup ends when the dashboard opens and shows your data. The first campaign run happens with Evgeni watching.
- **Never use ordinary Chrome** for these accounts. If ACL refuses to continue, that is a protection — ask Evgeni instead of working around it.
- **Never send** `.env`, `oauth_client.json`, tokens, or account IDs. If you need help, send the ACL Doctor output.

---

## If something goes wrong

| Problem | Fix |
|---|---|
| `python is not recognized` | Reinstall Python with "Add to PATH", restart Windows |
| GitHub asks for a password | Sign in with the company GitHub account Evgeni gave access to |
| Doctor says a file is missing | Fix that exact path in `.env` — full path, forward slashes |
| Doctor says Multilogin profiles missing | Open each of your profiles once in Multilogin Desktop |
| Port 9090 is busy | Close the other ACL window |
| Dashboard closes immediately | Run `start_dashboard.bat` again, photograph the last line, send it to Evgeni |

---

## When you are done, send Evgeni

- A screenshot of the ACL Doctor result
- Confirmation the dashboard opened and showed your accounts
- Any error text, and which step number it happened on
