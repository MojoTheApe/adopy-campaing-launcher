---
kind: spec
name: global-rollout-plain-english
created: 2026-07-22
status: draft
version: 1
tags:
  - overview
  - non-technical
related:
  - global-rollout-overview.md
  - hub-agent-architecture.md
  - agent-update-strategy.md
---

# Moving to a shared setup — plain-English explanation

A non-technical companion to `global-rollout-overview.md` and
`hub-agent-architecture.md`. Written for anyone who wants the "what's
happening and why" without the implementation details.

## The picture today

Right now, everything is **one laptop = the whole business**. Your
laptop runs the dashboard, holds the data, and runs the robot that
clicks around in Facebook Ads Manager. To let the whole company use
this, we need to "move out" each of those pieces into its own place
so many people can use them at the same time.

## The process, step by step

**1. Move the dashboard onto the internet**
Right now the site only opens on your own computer
(`127.0.0.1:9090`). It needs to move onto a server on the internet, so
an address like `dashboard.yourcompany.com` works for every employee,
not just you.

**2. Move the data into one shared database**
Right now data lives in scattered pieces: a CSV file on your laptop,
a Google Sheet, a database file on your own disk. All of that needs
to come together into **one shared database** that also lives on the
internet, next to the dashboard. Then everyone looks at the same
numbers instead of everyone having their own copy.

**3. Each employee gets their own "worker program"**
The robot that clicks through Ads Manager (Multilogin + automation)
can't live on a shared server — it needs an actual computer with a
specific person's browser profile and accounts open. So each employee
installs a small "agent" program on their own machine. It asks the
shared website: "any work for me?" — and if yes, it does the clicking
itself, through Multilogin, **on that person's own machine, with
their own accounts**.

**4. Logging in**
Instead of everyone getting a brand-new password for this internal
site, it makes sense to let people log in with the same Google/
Microsoft account they already use at work.

**5. Keeping everyone's program up to date**
When you (or another developer) fix something in the code, that fix
needs to reach every employee's agent program automatically, without
anyone manually downloading anything — the same way Chrome quietly
updates itself.

**6. Roll out gradually, not to everyone at once**
Try it with 2-3 people first, see what breaks, fix it — then hand it
out to the whole company. The normal way to launch any new internal
tool.

## The pieces of infrastructure needed

| Piece | Why, in plain terms |
|---|---|
| **A server / cloud** (e.g. Azure) | A computer on the internet that runs all the time and hosts the dashboard — instead of your personal laptop |
| **A domain** (web address) | So the site has a real address like `campaigns.company.com` instead of `127.0.0.1` |
| **A shared database** (e.g. Postgres) | One shared "spreadsheet on steroids" on the internet that everyone reads and writes to at once, without version confusion |
| **File storage** | A place for the screenshots and logs the robot produces while working — so anyone can see what went wrong without walking over to a coworker's desk |
| **A login system (SSO)** | So people sign in with their existing work Google/Microsoft account instead of a brand-new password |
| **The agent program** (not a "service" — software on each employee's machine) | The thing that actually clicks through Facebook using that person's accounts |
| **An auto-update mechanism for the agent** | So code fixes reach every employee automatically, with no manual steps |

At the core, this is the same three things discussed earlier — a
shared website, a shared database, and an agent on each machine —
this table is just the concrete "building blocks" underneath those
three things.

#overview #non-technical #rollout
