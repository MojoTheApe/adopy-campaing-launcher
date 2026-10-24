# ACL — Setup on a New Computer

Windows 10/11 (64-bit). About 10 minutes. No coding, no terminal, no GitHub
account, no Python or Git to install.

---

## Step 0 — Have these ready

- [ ] **The installer file** `Adopy-Campaign-Launcher-Setup-<version>.exe`, which you get from Evgeni.
- [ ] **Your name exactly as it is written in the CM column of the Stats workbook**
- [ ] **The Stats workbook** (Excel), saved somewhere on this computer
- [ ] **Multilogin Desktop**, installed and signed in, and your **Multilogin automation token**

Get the token through the password manager, never by chat or email.

You no longer need `Upload FB.xlsx`, a Google Sheet link, `oauth_client.json`,
or a GitHub account. Shared targets and ad texts require support to configure
the team's private library on this computer. Until that is done, the library
is local to this computer; finishing this wizard does not enable sharing.

---

## Step 1 — Run the installer

Double-click `Adopy-Campaign-Launcher-Setup-<version>.exe`.

- It installs for your Windows user only, so it does not need administrator rights.
- Leave **Create a desktop shortcut** ticked.
- Windows may show *"Windows protected your PC"*. Click **More info → Run anyway**.
  This happens only because the installer is not code-signed yet.

When it finishes, leave **Open Adopy Campaign Launcher** ticked and click **Finish**.

---

## Step 2 — First-run setup

The setup window opens by itself the first time. It has these steps:

1. **System check.** ACL checks Windows, free disk space and port 9090 for you.
2. **Your name.** Type it exactly as in the CM column. ACL finds your ad accounts
   in the workbook by this name.
3. **Local files.**
   - **Stats workbook:** pick your Excel file with **Browse…**.
   - **Creatives folder:** it is already filled in as `Documents\ACL\Creatives`,
     and ACL creates the folder. Keep it, or pick another folder.
4. **Multilogin.** Make sure Multilogin Desktop is open and signed in. Paste your
   automation token, then click **Test Multilogin connection**.
5. **Readiness → Finish.** Every line must show a ✓.

Nothing is published during setup, and no campaign is touched.

You can run setup again at any time from **Start → Adopy Campaign Launcher →
Setup Campaign Launcher**. It keeps your accounts and connected profiles.

---

## Step 3 — Put your creatives in

Copy your videos and images into the Creatives folder. By default that is
`Documents\ACL\Creatives`. ACL searches it and all its subfolders by file name.

To use a different folder, or to point ACL at a different Stats workbook later,
go to the dashboard → **Accounts** → **This computer**, paste the path, and click
**Save**. To copy a path: in Explorer, hold Shift, right-click the file or folder,
and choose **Copy as path**.

---

## Step 4 — Connect your accounts

The dashboard opens in your browser at `http://127.0.0.1:9090`. Next time, open it
with the **Adopy Campaign Launcher** shortcut on the desktop.

Open **Accounts** and connect each of your accounts to its Multilogin profile:
find the profile by the last letters of its name, open it if needed, and click
Connect. ACL reads the ad account from the profile, so you type no IDs.

---

## Keeping ACL up to date

When a new version is out, the dashboard header shows **Update to <version>**.
Hover over it or click it to see what changed. Click **Update** and confirm.

- Wait for a campaign run to finish first. While a run is in progress, ACL
  refuses to update.
- ACL downloads the new version, checks that the file is exactly the published
  one, installs it and restarts. The page reloads by itself after a few minutes.
- If the new version does not start, ACL tries to restore the previous version
  and tells you the result. If recovery also fails, contact support; the error
  does not claim that the old version was restored.

Nothing updates on its own, and your name, files, accounts, profiles and tokens
are never touched. They live in `%LOCALAPPDATA%\ACL`, outside the program folder.

If Evgeni sends you an installer file instead, stop ACL first. Go to **Start →
Adopy Campaign Launcher → Stop Campaign Launcher**; closing the browser tab alone
does not stop it. Then run the new file.

---

## Rules while setting up

- **Do not publish anything.** Setup ends when the dashboard opens and shows your data. The first campaign run happens with Evgeni watching.
- **Never use ordinary Chrome** for these accounts. If ACL refuses to continue, that is a protection. Ask Evgeni instead of working around it.
- **Never send** tokens, account IDs or anything from `%LOCALAPPDATA%\ACL`. If you need help, use **Start → Adopy Campaign Launcher → Create diagnostic bundle** and send the ZIP it makes. Tokens and account identifiers are removed from it.

---

## If something goes wrong

| Problem | Fix |
|---|---|
| "… is not in the CM column of this Stats workbook" | Your name does not match the CM column. Go back to the name step and type it as shown in the "Did you mean" hint or copy it from the workbook exactly (capitals do not matter). |
| "Stats workbook is missing columns" | You picked a different Excel file. Pick the Stats workbook. |
| "Cannot reach Multilogin Launcher" | Open Multilogin Desktop, sign in, and test again. |
| "Multilogin rejected token" | The token is wrong or expired. Get a new one through the password manager. |
| Port 9090 is busy | ACL is already running. Open the shortcut, or use **Stop Campaign Launcher** first. |
| The dashboard does not open | Run **Diagnostics** from the Start menu, then send Evgeni the diagnostic bundle. |

---

## When you are done, send Evgeni

- A screenshot of the dashboard showing your accounts
- Any error text, and which step it happened in
