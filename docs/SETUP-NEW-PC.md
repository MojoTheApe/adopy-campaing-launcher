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
   - **Stats workbook:** pick your Excel file with **Browse…**.
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

The dashboard opens in your browser at `http://127.0.0.1:9090`. Next time, open it
with the **Adopy Campaign Launcher** shortcut on the desktop.

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
| System check | ✕ *Local dashboard port*, *port 9090 is already in use* | ACL is already running. Use **Start → Adopy Campaign Launcher → Stop Campaign Launcher**, then click **Back** and **Next**. |
| System check | *Resolve failed system checks: …* after **Next** | One of the lines above is ✕. Fix it as this table says for that line. |
| Your name | *Enter your name exactly as it appears in the Stats workbook.* | The name field is empty. Type your name. |
| Your name | *Replace the example campaign manager name with your own name.* | Type your own name, not the example. |
| Your name | *Campaign manager name is too long.* | Type only your name, as in the CM column. |
| Local files | *Stats workbook does not exist.* | Pick the file again with **Browse…**. |
| Local files | *Stats workbook must be an .xlsx or .xlsm file.* | You picked a different file. Pick the Stats workbook. |
| Local files | *Stats workbook cannot be opened: …* | Close the workbook in Excel, then click **Next** again. |
| Local files | *Stats workbook is missing columns: …* or *Stats workbook is missing CM or Account Alias columns.* | You picked a different Excel file. Pick the Stats workbook. |
| Local files | *No accounts were found for this campaign manager in the Stats workbook.* | Your name does not match the CM column. Click **Back** and copy it from the workbook exactly. |
| Local files | *Creatives folder could not be created: …* | Pick another folder with **Browse…**. |
| Local files | *Creatives folder does not exist.* or *Creatives folder is not readable.* | Pick another folder with **Browse…**, or keep the suggested `Documents\ACL\Creatives`. |
| Readiness | *Go back and confirm your name and the Stats workbook.* | Click **Back** twice and click **Next** on each step again. |
| Readiness | *Existing account map is invalid…*, *…has malformed rows…*, *…has duplicate names…* or *Account ID changed for …* | ACL was set up here before and its saved accounts disagree with the workbook. Do not delete anything. Tell Evgeni. |
| Accounts | *Email and password, please* | One of the two fields is empty. |
| Accounts | *This login has several Multilogin workspaces — choose the one with your profiles.* | Pick the workspace where your browser profiles are, then continue. |
| Accounts | *Signed in, but no automation token was saved. Ask the workspace owner for a token.* | Your Multilogin plan or role does not allow automation. Tell Evgeni. |
| Accounts | *Token saved, but connection could not be verified.* | Open Multilogin Desktop, sign in, and try again. |
| Any | The dashboard does not open | Run **Diagnostics** from the Start menu, then send Evgeni the diagnostic bundle. |
| Any | Something not in this table | Write down which step you were on and what the screen said, and tell Evgeni. That is a gap in this guide, and it will be fixed. |

---

## When you are done, send Evgeni

- A screenshot of the Accounts screen showing your connected accounts
- Any error text, and which step it happened in
- Every place where this guide said one thing and the screen showed another
