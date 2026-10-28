# ACL — Setup on a New Computer

Windows 10/11 (64-bit). About 10 minutes. No coding, no terminal, no GitHub
account, no Python or Git to install.

This guide comes with the program. After installing, it is in **Start → Adopy
Campaign Launcher → Setup guide**, and the setup window has a **Setup guide**
button on every step. Every step below ends with what you should see. If you see
something else, look in **If something goes wrong** at the end before asking.

---

## Step 0 — Have these ready

- [ ] **The installer file** `Adopy-Campaign-Launcher-Setup-<version>.exe`, which you get from Evgeni.
- [ ] **Your name exactly as it is written in the CM column of the Stats workbook**
- [ ] **The Stats workbook** (Excel), saved somewhere on this computer
- [ ] **Multilogin Desktop**, installed, open and signed in
- [ ] **Your Multilogin email and password.** You type them into ACL in Step 4, not in the setup window.

If Evgeni gives you a Multilogin automation token instead, get it through the
password manager, never by chat or email.

You no longer need `Upload FB.xlsx`, a Google Sheet link, `oauth_client.json`,
or a GitHub account. Shared targets and ad texts require support to configure
the team's private library on this computer. Until that is done, the library
is local to this computer; finishing this wizard does not enable sharing.

---

## Step 1 — Run the installer

Double-click `Adopy-Campaign-Launcher-Setup-<version>.exe`.

- It installs for your Windows user only, so it does not need administrator rights.
- Windows may show *"Windows protected your PC"*. Click **More info → Run anyway**.
  This happens only because the installer is not code-signed yet.
- Leave **Create a desktop shortcut** ticked.

**You should see:** a last screen that says setup has finished, with two ticked
boxes: **Open the setup guide** and **Open Adopy Campaign Launcher**. Leave both
ticked and click **Finish**. This guide opens in your browser, and the setup
window opens next to it.

**If you see something else:**

- *"Windows protected your PC"* without a **Run anyway** button: your company
  policy blocks unsigned programs. Stop and tell Evgeni.
- The installer says Adopy Campaign Launcher is running and must be closed: let
  it close the program, or use **Start → Adopy Campaign Launcher → Stop Campaign
  Launcher** first.

---

## Step 2 — First-run setup

The setup window opens by itself the first time. Its title bar says
**Adopy Campaign Manager Setup**, and the top of the window shows the step name
and *Step N of 6*. Nothing is published during setup, and no campaign is touched.

1. **Welcome.** Shows the version. Click **Next**.
2. **System check.** ACL checks four things for you: *Windows x64*, *Writable
   ACL data folder*, *Free disk space* and *Local dashboard port*.
   **You should see:** four green ✓ lines. Click **Next**.
   A red ✕ line has its reason next to it; see the table below.
3. **Your name.** Type it exactly as in the CM column. ACL finds your ad accounts
   in the workbook by this name. Click **Next**.
4. **Local files.** Fill in both, then click **Next**. ACL opens the workbook
   and looks for your name. If it cannot, a red line appears at the bottom of
   the window and you stay on this step.
   - **Stats workbook:** pick your Excel file with **Browse…**. If ACL finds it in
     the usual place, it is already filled in (shown in green); change it only if
     you were sent a different file. It is the Excel report your team lead keeps;
     ACL does not create or download it.
   - **Creatives folder:** it is already filled in as `Documents\ACL\Creatives`,
     and ACL creates the folder. Keep it, or pick another folder.
5. **Readiness.** **You should see:** green ✓ next to *Runtime*, *Your accounts*,
   *Local files* and *Safety mode*, and a line *Found N account(s) for this
   campaign manager*. Check that N is the number of accounts you work with.
   Click **Finish**.
6. **Finish.** **You should see:** *Adopy Campaign Manager is ready on this
   computer* in green. Click **Close**. The dashboard opens in your browser.

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

## Step 4 — Connect Multilogin and your accounts

The dashboard opens in your browser by itself, at `http://127.0.0.1:9090` (if that port
is taken by another program on the computer, ACL uses the next free one, up to 9109).
Next time, open it with the **Adopy Campaign Launcher** shortcut on the desktop.

1. Open **Accounts**. Under your accounts there is a line
   *Multilogin: …* with a **Connect Multilogin** button.
2. Click **Connect Multilogin** and sign in with your Multilogin email and
   password. If your login has several Multilogin workspaces, ACL asks you to
   choose one: pick the one where your browser profiles are.
   **You should see:** *Connected to workspace …* and then *Reload to search
   profiles*. Reload the page.
3. Connect each account to its Multilogin profile: find the profile by the last
   letters of its name, open it if needed, and click **Connect**. ACL reads the
   ad account from the profile, so you type no IDs.

**Setup is done when** every one of your accounts on the Accounts screen shows
its connected profile. Stop here and send Evgeni a screenshot.

---

## Keeping ACL up to date

When a new version is out, the dashboard header shows **Update to <version>**.
Hover over it to see what changed. Click it, read the **Update ACM to <version>?**
window, and click **Update**. The button is not shown when you are up to date.

- Wait for a campaign run to finish first. While a run is in progress, ACL
  refuses to update.
- ACL downloads the new version, checks that the file is exactly the published
  one, installs it and restarts. The page reloads by itself after a few minutes.
- If the new version does not start, ACL tries to restore the previous version
  and tells you the result. If recovery also fails, contact support; the error
  does not claim that the old version was restored.

Nothing updates on its own, and your name, files, accounts, profiles and tokens
are never touched. They live in `%LOCALAPPDATA%\ACL`, outside the program folder.
Each update also brings the current version of this guide.

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

The messages in the middle column are what ACL shows, word for word.

| Where | What you see | What to do |
|---|---|---|
| Installer | *Windows protected your PC* | Click **More info → Run anyway**. No **Run anyway** button: tell Evgeni. |
| System check | ✕ *Windows x64* | ACL needs 64-bit Windows 10 or 11. Tell Evgeni. |
| System check | ✕ *Writable ACL data folder* | Your Windows user cannot write to `%LOCALAPPDATA%\ACL`. Tell Evgeni and send the reason shown next to it. |
| System check | ✕ *Free disk space* | ACL needs at least 500 MB free. Free up space, click **Back**, then **Next** to check again. |
| System check | ✕ *Local dashboard port*, *ports 9090-9109 are all used by other applications* | Rare: another program is holding every port ACL may use. Close other programs that serve web pages, then click **Back** and **Next**. A busy port 9090 alone is not an error: ACL then uses the next free port and shows a ✓ *port … is available*. |
| System check | *Resolve failed system checks: …* after **Next** | One of the lines above is ✕. Fix it as this table says for that line. |
| Your name | *Enter your name exactly as it appears in the Stats workbook.* | The name field is empty. Type your name. |
| Your name | *Replace the example campaign manager name with your own name.* | Type your own name, not the example. |
| Your name | *Campaign manager name is too long.* | Type only your name, as in the CM column. |
| Local files | *No Stats workbook chosen.* or *Stats workbook does not exist.* | The Stats workbook is the Excel report your team lead keeps. Ask your team lead for the current file, save it on this computer, and pick it with **Browse…**. |
| Local files | *Stats workbook must be an .xlsx or .xlsm file.* | You picked a different file. Pick the Stats workbook. |
| Local files | *Stats workbook cannot be opened: …* | Close the workbook in Excel, then click **Next** again. |
| Local files | *Stats workbook is missing columns: …*, *Missing columns in Excel: …*, *Stats workbook has duplicated columns: …* or *Stats workbook is missing CM or Account Alias columns.* | You picked a different Excel file. Pick the Stats workbook. |
| Local files | *Stats workbook is empty: no header row found* or *Stats workbook is not usable: …* | The file has no table in it, or is not the real workbook. Pick the Stats workbook again with **Browse…**. |
| Local files | *… is not in the CM column of this Stats workbook, so the dashboard would show no accounts.* | Your name does not match the CM column. The message may add *Did you mean: …*. Click **Back**, type the name as shown there or copy it from the workbook exactly (capitals do not matter). |
| Local files | *Creatives folder could not be created: …* | Pick another folder with **Browse…**. |
| Local files | *Creatives folder does not exist.* or *Creatives folder is not readable.* | Pick another folder with **Browse…**, or keep the suggested `Documents\ACL\Creatives`. |
| Readiness | *Go back and confirm your name and the Stats workbook.* | Click **Back** twice and click **Next** on each step again. |
| Readiness | *Existing account map is invalid…*, *…has malformed rows…*, *…has duplicate names…* or *Account ID changed for …* | ACL was set up here before and its saved accounts disagree with the workbook. Do not delete anything. Tell Evgeni. |
| Accounts | *Email and password, please* | One of the two fields is empty. |
| Accounts | *This login has several Multilogin workspaces — choose the one with your profiles.* | Pick the workspace where your browser profiles are, then continue. |
| Accounts | *Signed in, but no automation token was saved. Ask the workspace owner for a token.* | Your Multilogin plan or role does not allow automation. Tell Evgeni. |
| Accounts | *Token saved, but connection could not be verified.* | Open Multilogin Desktop, sign in, and try again. |
| Accounts | *Multilogin token expired at … Profiles cannot start until you sign in again.* or *Multilogin token (…) expires at … Sign in again to renew it.* (red or amber banner at the top of the dashboard) | Your Multilogin sign-in has expired or is about to. Open **Accounts**, click **Connect Multilogin** and sign in again with your Multilogin email and password. |
| Update | No **Update** button in the header | Nothing is waiting: you are on the newest version, or the dashboard could not check (offline, or it was not started from the desktop shortcut). Start ACL from the desktop shortcut and reload the page later. |
| Update | *Update not started: … campaign job(s) still running…*, *Wait for campaign jobs to finish before updating.* or *Campaign jobs started during the download; try again when they finish.* | A campaign run is in progress. Wait until it finishes, then click **Update** again. |
| Update | *Update not started: Wait for browser reading to finish before updating.* | ACL is reading from a browser profile. Wait a few minutes and try again. |
| Update | *Update not started: An update is already in progress.* | Someone already clicked **Update** (maybe in another tab). Wait for the page to reload by itself. |
| Update | *Update not started: GitHub is unreachable: …*, *The update could not be downloaded: …* or *GitHub answered HTTP …* | No internet, or GitHub did not answer. Check your connection and try again in a few minutes. ACL keeps running on the current version. |
| Update | *Update not started: The update channel refused access: the build's release token is missing or expired.* or *No published release was found in the update channel.* | This installation can no longer read the release. Tell Evgeni; you may need a newer installer. |
| Update | *Update not started: The downloaded installer does not match its published SHA-256; it was discarded.* (also *The downloaded installer does not match its published SHA-256 size.*, *The update response is larger than expected.*, *The download redirect must use HTTPS.*, *The published release.json is not valid; nothing was installed.*, *The latest release has no …*, *The GitHub release response is invalid.*, *The release asset is not on the configured GitHub API.*, *The update channel configuration is invalid.*) | The downloaded file was not exactly the published one, so ACL refused to install it. Click **Update** again. If it repeats, tell Evgeni. |
| Update | *Update not started: The update helper could not start.* or *The update could not be prepared.* (ACM is still running) | Nothing was changed. Click **Update** again; if it repeats, run **Diagnostics** and send Evgeni the bundle. |
| Update | *Update not started: ACM is already on this release or a newer version.* | You are already up to date. Reload the page. |
| Update | *Update not started: Updates are not configured in this build.* or *Only the installed ACL.exe can install release updates.* | This copy of ACL cannot update itself. Run the newer installer Evgeni sends you (see **Keeping ACL up to date**). |
| Update | After clicking **Update**, *ACM did not come back within 10 minutes* | Open ACL again from the desktop shortcut. If the new version did not start, the previous one is restored automatically. If it still does not open, run **Diagnostics** and tell Evgeni. |
| Update | A message *The update to … did not work, so ACM went back to …* or *The update to … did not start: …* | The new version failed, and ACL put the previous one back. Nothing else changed. Tell Evgeni and send the reason shown. |
| Update | *The update failed and ACM could not fully restore the previous version.* | Do not retry. Run the installer you used before (or the one Evgeni sends), and tell Evgeni. |
| Any | The dashboard does not open | Run **Diagnostics** from the Start menu, then send Evgeni the diagnostic bundle. |
| Any | Something not in this table | Write down which step you were on and what the screen said, and tell Evgeni. That is a gap in this guide, and it will be fixed. |

---

## When you are done, send Evgeni

- A screenshot of the Accounts screen showing your connected accounts
- Any error text, and which step it happened in
- Every place where this guide said one thing and the screen showed another
