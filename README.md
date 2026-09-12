# Khoshzad — personal Discord bot for the weekly review, evening close, brag doc & Q&A

Khoshzad (خوش‌زاد) is Sajjad's personal Discord bot. Runs on the dev box as
`systemd --user` service `weekly-bot` — the service, directory and repo keep the plain
functional name; the bot's persona in conversation is Khoshzad. Owner-only. Answers and
write-mode jobs come from headless `claude -p` on your own subscription. Schedule:
**morning brief** Sun–Thu 08:00, **evening close** Sun–Thu 17:30, **weekly report +
brag document + snippet** Thu 19:00 — all Asia/Tehran.

## One-time setup (you)

### 1. Discord application (personal — not the Notify-Me team bot)
1. https://discord.com/developers/applications → **New Application** → name it.
2. **Bot** tab → **Reset Token** → copy it. Under *Privileged Gateway Intents* enable **Message Content Intent**.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions *Send Messages, Embed Links, Attach Files, Read Message History*. Open the generated URL, add the bot to **your own** server.
4. Once the bot is online, run `/whoami` in Discord to get `OWNER_ID` and `CHANNEL_ID` — no need to hunt for Developer Mode.

### 2. Obsidian vault via the "Git" community plugin
1. Obsidian: Settings → Community plugins → browse → search **Git** (by Vinzent, formerly denolehov; NOT "Obsidian Git" — the listing was renamed) → install + enable.
2. Point it at a **private** repo; enable auto commit+push and pull on startup.
3. Give this box **read** access first: a deploy key. The bot already has one generated (`~/.ssh/obsidian-vault-deploy` / `~/.ssh/obsidian-vault-github` alias) — add its public key as a repo deploy key.
4. **Evening close and the brag document need write access.** GitHub deploy keys can't be upgraded in place — delete the existing key on the repo and re-add the **same public key** with *Allow write access* ticked (same keypair, no new key to generate):
   ```
   cat ~/.ssh/obsidian-vault-deploy.pub
   ```
5. Clone/verify: `git clone git@obsidian-vault-github:<you>/<vault-repo>.git ~/obsidian-vault` (already done once; re-run only if missing). The bot pulls before every job and pushes as local identity `weekly-bot <weekly-bot@sajjad.local>` — distinct from your own "vault backup" commits, already configured (`git -C ~/obsidian-vault config user.name/user.email`, local to this repo only).

### 3. Install the service
```bash
bash ~/weekly-bot/install.sh          # venv (via uv) + discord.py + unit; writes ~/.config/weekly-bot/.env from env.example
nano ~/.config/weekly-bot/.env        # DISCORD_BOT_TOKEN, OWNER_ID, CHANNEL_ID
systemctl --user restart weekly-bot
journalctl --user -u weekly-bot -f    # expect: "ready as <bot> · owner … · channel …"
```

## Use

| Command | Does |
|---|---|
| `/weekly [sunday]` | Full report for that week (default: current). Posts the embed + `report.html`, then auto-chains `/brag` and `/snippet` for the same week. |
| `/brief [date]` | Morning brief for that date (default: today). On the live default run, if today's worklog page doesn't exist yet it's created in your own `Main tasks:` format — seeded with yesterday's unfinished main tasks, exactly what you'd retype by hand — and pushed. Then shows the plan, your full open-reviewer-MR queue (flagged past 24h), and a live Jira lookup for open issues assigned to you. A manual `/brief <past-date>` never creates a file — read-only in that case. |
| `/close [date]` | Evening close for that date (default: today). Reconciles the day's worklog against real commits/MRs, ticks what's supported, appends unplanned work, pushes to the vault, posts a diff embed. |
| `/brag [sunday]` | Append evidence-backed wins to `Brag/1405.md` in the vault, under Julia Evans' sections. Requires that week's report to exist. |
| `/snippet [sunday]` | Post a short Persian شد/آموختم/بعدی/گیر کردم update — Discord only, nothing written to the vault. |
| `/status` | Vault head, reports on disk, last evening close / weekly post, schedule. |
| `/whoami` | Your user ID and this channel's ID. |
| plain message / DM | Treated as a question — answered from `~/weekly-reports` + `~/obsidian-vault`. |

## How each job verifies itself

Evening close and `/brag` never trust the model's own "I did X" narrative for what
changed. The bot records the vault's git HEAD before the run and diffs it against HEAD
after: if nothing moved, it says so plainly ("no changes pushed" / "nothing new to
add") instead of repeating whatever the model claims. When something did change, the
Discord embed's content — the ticked/added lines, the brag lines — comes from the real
`git diff`, not from the model's summary. This is the same "verify, don't just trust
the self-report" discipline the workspace uses for dev-agent review.

## How it works

- **Morning brief** (`run_morning_brief`): on the live scheduled/default run (never for a manual
  `/brief <past-date>` lookup), if today's worklog page doesn't exist yet, it's created —
  `seed_todays_worklog` writes his own `Main tasks:` header plus one `- [ ]` per unfinished main
  task carried from the last work day (`unfinished_main_tasks`, which excludes evening close's own
  `(session)`/`(unplanned)` log entries — confirmed from his real history that only the untagged
  bullets get retyped by hand each morning), then commits and pushes — mechanical, no model
  involved. Never overwrites a file he already wrote. Your full open-reviewer MR queue comes from
  GitLab (`open_reviewer_mrs`, age = time since the MR was opened, flagged 🔴 past 24h). The one
  thing that needs `claude -p` is the live Jira lookup (`assignee = currentUser() AND
  statusCategory != Done`) via the Atlassian MCP — a narrow, read-only tool call, not a judgment call.
- **Evening close** (`run_evening_close`): resolves today's Jalali date (`jalali.py`,
  1405 anchor table), pre-fetches today's commits and MRs itself (reusing
  `weekly_report.py`'s `commits()`/`fetch_mrs()` — one source of truth), and hands
  `claude -p` only the judgment call: reconcile the checklist, create the file if it
  doesn't exist, commit, push. Tools are scoped to `Read/Write/Edit/Glob` plus git
  commands *restricted to the vault path* — it cannot touch another repo or run an
  arbitrary git command. The chart's BF/AP/FD/WD split carves review time out into
  its own line (`review_wall_split`) instead of leaving it inside whichever category
  the session was classified as — grounded in real GitLab reviewer-role MR numbers
  (`!1607`) appearing in the session, not a keyword match: this workspace's own
  product is a code-review pipeline, so keyword matching on "review" badly
  over-counted (one real day hit 100% "review" purely from mentioning `/code-review`,
  its own routine self-review step, nowhere near what he'd actually spent reviewing
  someone else's code).
- **Brag document** (`run_brag`): reads that week's `metrics.json` (+ `narrative.html`
  for numbers/quotes already on record) and appends lines to `Brag/1405.md` under
  Evans' sections (Projects · Collaboration & mentorship · Design & documentation ·
  Company building · What you learned · Outside of work · Fuzzy work). Never invents a
  number; skips a subject with no evidence.
- **Weekly snippet** (`run_snippet`): read-only, no vault write — a short Persian
  status for teammates, built from the week's report.
- **Scheduler**: checked every minute in Asia/Tehran; each job fires once per
  day/week (`state.json`), and the scheduler skips silently (no repeated "busy"
  messages) if a run is already in progress. A failed scheduled run never kills the
  loop — it's caught, logged, and DMs you so you know without checking `journalctl`.

## Notes

- Token lives only in `~/.config/weekly-bot/.env` (600). Nothing here is in a project repo.
- `vault-map.md` (the CLAUDE.md `@`-import documenting the vault's structure) is git-ignored —
  it names real coworkers and internal project codenames. It stays local; a fresh clone needs it
  copied back in by hand for the `@vault-map.md` import to resolve.
- Cost: each answer/job is a Claude Code run on your subscription. Evening close and
  brag are full write-mode runs; the plain Q&A and snippet are read-only and cheaper.
- Restart after changing `.env`: `systemctl --user restart weekly-bot`.
- If write access isn't granted yet, `/close` and `/brag` still run and still commit
  *locally* — a local `git commit` doesn't need remote write access, only `git push`
  does. The bot checks this explicitly (local HEAD vs. `origin`'s HEAD, via
  `vault_pushed()`) rather than trusting that a moved HEAD means a successful push, so
  you'll see an explicit ⚠️ "not pushed — deploy key is likely still read-only"
  instead of a false success. Nothing is lost: the local commit stays, and the very
  next successful push (after the key is upgraded) carries it along automatically —
  the prompt always attempts `git push` at the end, whether or not it just committed.
