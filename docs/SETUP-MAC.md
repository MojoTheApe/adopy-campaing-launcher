# ACL — Setup on a Mac

Apple Silicon or Intel Mac. About 15 minutes. Windows: see
[SETUP-NEW-PC.md](SETUP-NEW-PC.md).

> **Status.** The dashboard (statistics, accounts, campaign builder, updates)
> is ported. Everything that talks to Multilogin goes through its local
> launcher API, which is the same on macOS. Not ported: anything that drives
> the Mimic `chrome.exe` directly, or reads Windows window titles — see
> *Known limits* at the bottom.

---

## Step 0 — Ask Evgeni for these first

Same list as Windows. They are deliberately not in GitHub.

- [ ] GitHub access to `MojoTheApe/adopy-campaing-launcher`
- [ ] **Your CM name** — exact spelling from the stats workbook
- [ ] **`oauth_client.json`**
- [ ] **Multilogin** login and the automation token (Multilogin **for Mac** must be installed)
- [ ] The three work files: `cursor example.xlsx`, `Upluad FB.xlsx`, `Creatives` folder

Get the token and the OAuth file through the password manager — never by chat or email.

---

## Step 1 — Run one file

Ask Evgeni for **`Install ACL.command`** and run it from Terminal (the main way):

```bash
bash "$HOME/Downloads/Install ACL.command"
```

If you double-click it instead and macOS refuses (*"cannot be opened because it is from an
unidentified developer"*): open **System Settings → Privacy & Security**, scroll down and press
**Open Anyway** next to the file name.

If macOS asks whether Terminal may access your Desktop or Documents, press **Allow** — otherwise
ACL cannot see your work files. To fix it later: **System Settings → Privacy & Security →
Files and Folders** → enable Terminal.

It checks Git and Python 3.11+ (offering Homebrew's Python if missing), downloads
ACL into `~/Documents/ACL` at the promoted release, creates a Desktop shortcut,
installs ACL into its own `.venv`, checks the machine and opens the dashboard.

- **Git missing:** macOS opens the *Command Line Tools* installer. Finish it, then run the file again.
- **GitHub sign-in:** use the company account. If it asks for a password in Terminal, that is a
  token, not the account password: `brew install gh && gh auth login` is the easy way.
- It is safe to run again at any time: an existing install is updated, never re-downloaded, and
  your settings are never touched.

---

## Step 2 — Put your files and your name in

**A.** Put these on your Desktop with exactly these names:

```
cursor example.xlsx
Upluad FB.xlsx
Creatives          (the folder)
```

**B.** Open the machine folder and copy `oauth_client.json` into it:

```bash
open "$HOME/Library/Application Support/ACL"
```

**C.** Open `.env` in that folder and set one line:

```
MY_CM_NAME=Your Name Exactly As In The Workbook
```

> The **Multilogin token** is pasted into the dashboard (Multilogin panel), not into this file.

---

## Step 3 — Check the machine (only if something looks wrong)

```bash
cd ~/Documents/ACL
.venv/bin/python scripts/acl_doctor.py
```

It must end with `All required items present.` A `[warn] Telemetry upload` line is fine;
a `[FAIL]` line says exactly what is missing.

---

## Step 4 — Start the dashboard

Double-click **Adopy Campaign Manager** on the Desktop (or run `bash ~/Documents/ACL/start_dashboard.sh`).

Keep the Terminal window open — closing it stops ACL. The browser opens
`http://127.0.0.1:9090`. The first load takes a minute or two.

## Step 5 — Connect your accounts

Dashboard → **Accounts** → pick your open Multilogin profile for each account.

---

## Updates

Same as Windows: an **Update** button appears in the dashboard header when a verified release
exists; press it and ACL restarts into the new version, or puts the old one back by itself.
Nothing updates on its own.

## Rules while setting up

- **Do not publish anything.** Setup ends when the dashboard opens and shows your data.
- **Never use ordinary Chrome** for these accounts.
- **Never send** `.env`, `oauth_client.json`, tokens or account IDs. Send the ACL Doctor output instead.

## If something goes wrong

| Problem | Fix |
|---|---|
| `Install ACL.command` will not open | Run `bash "Install ACL.command"` |
| `python3: command not found` / too old | `brew install python@3.12`, open a new Terminal window |
| Doctor: Multilogin profiles missing | Open each profile once in Multilogin for Mac. If its folder is not `~/mlx`, set `MLX_HOME` in `.env` |
| Port 9090 is busy | Close the other ACL window (`lsof -nP -iTCP:9090 -sTCP:LISTEN` shows what holds it) |
| Dashboard closes immediately | Run `.venv/bin/python serve.py` in `~/Documents/ACL`, send the last lines to Evgeni |

## Known limits (macOS)

- **Account picker names.** On Windows the picker also reads Multilogin window titles; on a Mac
  it relies on Multilogin's launcher API only, so a name can show as an id when the API does not
  return it.
- **Opening profiles on Mac** is verified only after the Mac acceptance run.
- **Not verified on a real Mac yet.** The first install on a Mac is a test run: have Evgeni watching,
  and send the whole Terminal output if anything fails.
