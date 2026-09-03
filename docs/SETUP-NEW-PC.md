# ACL — Setup on a New Computer

Windows 10/11. About 30 minutes. No coding needed.

**Release to install: `acl-release-2026-09-03`**

---

## Step 0 — Ask Evgeni for these first

You cannot finish without them. They are deliberately not in GitHub.

- [ ] GitHub access to `MojoTheApe/adopy-campaing-launcher`
- [ ] **Your CM name** — exact spelling from the stats workbook
- [ ] **`oauth_client.json`** file
- [ ] **Multilogin** login, your profile names, and the automation token
- [ ] The three work files: `cursor example.xlsx`, `Upluad FB.xlsx`, `Creatives` folder

Get the token and the OAuth file through the password manager — never by chat or email.

---

## Step 1 — Install three programs

**Why these:** ACL is a Python program, so a machine without Python has nothing
to run it with. Git is how ACL is downloaded **and how it updates** —
the dashboard offers an **Update** button when a verified new release
exists, and refuses anything that is not one. Both are one-time installs.

Open **PowerShell** from the Start menu and paste these two lines:

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.13 -e
```

Then **close PowerShell and open it again**, and check both are there:

```powershell
git --version
python --version
```

You should see a version number for each. If `python` is not recognised even
after reopening PowerShell, restart Windows and try again.

<details>
<summary>If winget is not available on this PC</summary>

Download and run the installers by hand:

- **Git for Windows** — https://git-scm.com/download/win — default options are fine
- **Python 3.11 or newer, 64-bit** — https://www.python.org/downloads/ —
  **tick "Add python.exe to PATH"** on the first installer screen

</details>

**Third program: Multilogin Desktop.** Install it, sign in to the workspace,
open each of your assigned profiles once so it exists on this PC, close them,
and leave Multilogin Desktop running.

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

## Step 4 — Put your files and your name in

**A. Put these three on your Desktop, with exactly these names:**

```
cursor example.xlsx
Upluad FB.xlsx
Creatives          (the folder)
```

ACL looks there by itself. Nothing to configure — as long as the names match.

**B. Copy `oauth_client.json`** (the file from Evgeni) into the machine folder:

```powershell
explorer $env:LOCALAPPDATA\ACL
```

**C. Open `.env` in that same folder** and set one line:

```
MY_CM_NAME=Your Name Exactly As In The Workbook
```

Save it as `.env`, not `.env.txt`.

> The **Multilogin token** is not typed into this file — you paste it into the
> dashboard in Step 6, in the Multilogin panel.

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

In the dashboard, open the **Accounts** section and pick your open Multilogin profile for each account. That is all — you do **not** need to edit any spreadsheet.

---

## Keeping ACL up to date

When a new verified release exists, an **Update** button appears in the
dashboard header, next to Refresh. Press it and confirm. ACL restarts itself and
the page reloads on its own — usually under a minute. Do not close the black
window while it works.

If the new version does not start, ACL puts the old one back by itself and says
so in the black window. You are never left without a working dashboard.

The button is absent when there is nothing to install, and it refuses while a
campaign run is in progress — a restart mid-run would leave a half-built
campaign in Ads Manager.

Nothing updates on its own. Nothing you configured is touched: your name, files,
accounts and tokens live outside the ACL folder.

---

## Rules while setting up

- **Do not publish anything.** Setup ends when the dashboard opens and shows your data. The first campaign run happens with Evgeni watching.
- **Never use ordinary Chrome** for these accounts. If ACL refuses to continue, that is a protection — ask Evgeni instead of working around it.
- **Never send** `.env`, `oauth_client.json`, tokens, or account IDs. If you need help, send the ACL Doctor output.

---

## If something goes wrong

| Problem | Fix |
|---|---|
| `python is not recognized` | Close and reopen PowerShell. Still missing — restart Windows. |
| GitHub asks for a password | Sign in with the company GitHub account Evgeni gave access to |
| Doctor says a file is missing | Check the name on your Desktop matches exactly, including the space in `cursor example.xlsx` |
| Doctor says Multilogin profiles missing | Open each of your profiles once in Multilogin Desktop |
| Port 9090 is busy | Close the other ACL window |
| Dashboard closes immediately | Run `start_dashboard.bat` again, photograph the last line, send it to Evgeni |

---

## When you are done, send Evgeni

- A screenshot of the ACL Doctor result
- Confirmation the dashboard opened and showed your accounts
- Any error text, and which step number it happened on
