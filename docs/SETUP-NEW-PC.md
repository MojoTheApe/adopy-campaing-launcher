# ACL — Setup on a New Computer

Windows 10/11. About 15 minutes. No coding, no terminal.

**Which version gets installed:** the promoted release — the one tagged on the
production head with a green build. You do not pick it, and this page does not
name it: a number written here goes stale the moment the next release is
promoted, and a stale number is worse than none.

If the installer stops with *"Production has no verified annotated ACL release
tag"*, nothing is broken on your computer. It means master has been integrated
but not yet promoted to a release, and the installer refuses to put an untagged
branch on a working machine. Ask Evgeni to promote a release, then run the file
again.

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

## Step 1 — Run one file

Ask Evgeni for **`Install ACL.bat`** and double-click it.

It does everything that used to be four separate steps: installs Python and Git
if they are missing, downloads ACL, installs it, makes a desktop shortcut, checks
the machine, and opens the dashboard.

**The file never reopens itself.** One window, start to finish. If you see a
second black window appear, and then a third, you are running an **old copy** of
`Install ACL.bat` — one from before 2026-09-08, which reopened itself to pick up
a newly installed Python and could end up doing it over and over. Close all the
windows, ask Evgeni for the current file, and start again. (To check a copy
yourself: open it in Notepad and search for `NEEDRESTART`. The current file does
not contain that word.)

If a repair with `install_acl.bat` says the previous release was restored,
close that window and run `install_acl.bat` again. Recovery replaces the installer
too, so repair stops before using the restored files. It does not open another
window automatically.

**A GitHub sign-in window may open.** Use the company GitHub account you were
given access with.

It is safe to run again at any time: an existing installation is updated, never
re-downloaded, and your settings are never touched.

<details>
<summary>If winget is not available on this PC</summary>

The file will say so and stop without changing anything. Update **App Installer**
from the Microsoft Store and run it again, or install these two by hand and then
re-run it:

* Python 3.11+ — https://www.python.org/downloads/ (tick **Add python.exe to PATH**)
* Git — https://git-scm.com/download/win

</details>

---

## Step 2 — Put your files and your name in

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
> dashboard in Step 5, in the Multilogin panel.

---

## Step 3 — Check the machine (only if something looks wrong)

Step 1 already ran this check and showed the result. Run it again whenever
you change a file or a setting:

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

## Step 4 — Start the dashboard

Double-click **`start_dashboard.bat`**.

Keep the black window open — closing it stops ACL. The browser opens `http://127.0.0.1:9090`. The first load takes a minute or two while it reads the workbooks.

Google will ask you to sign in once. Use your company account and approve read-only access to Sheets.

---

## Step 5 — Connect your accounts

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
