#!/usr/bin/env python3
"""Personal Discord bot: weekly engineering review + Q&A over the reports and the Obsidian vault.

Owner-only. Every answer is a headless `claude -p` run on the owner's own subscription, with
~/weekly-reports and ~/obsidian-vault added as readable dirs. The weekly report is produced by
the /weekly-report skill (steps 0-5) and posted here as an embed + the report.html file.

Config: ~/.config/weekly-bot/.env (see README). State: ~/weekly-bot/state.json.
"""
import asyncio, collections, datetime as dt, json, logging, os, pathlib, re, shlex, subprocess, sys
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

import jalali

log = logging.getLogger("weekly-bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

HOME = pathlib.Path.home()


def env_int(name, default=0):
    raw = (os.environ.get(name) or "").split("#", 1)[0].strip()
    return int(raw) if raw.isdigit() else default


TOKEN = os.environ["DISCORD_BOT_TOKEN"].split("#", 1)[0].strip()
OWNER_ID = env_int("OWNER_ID")        # 0 = nobody is owner yet; only /whoami works
CHANNEL_ID = env_int("CHANNEL_ID")
REPORTS = pathlib.Path(os.environ.get("REPORTS_DIR") or HOME / "weekly-reports")
VAULT = pathlib.Path(os.environ.get("VAULT_DIR") or HOME / "obsidian-vault")
SCRIPT = pathlib.Path(os.environ.get("SKILL_SCRIPT") or HOME / ".claude/skills/weekly-report/scripts/weekly_report.py")
CLAUDE = os.environ.get("CLAUDE_BIN", "claude")
ROOT = pathlib.Path(os.environ.get("ROOT_DIR") or HOME / "Notify-Me")
GIT_AUTHOR = os.environ.get("GIT_AUTHOR", "Sajjad Vahedi")
GITLAB_USER = os.environ.get("GITLAB_USER", "sajjad.vahedi")

sys.path.insert(0, str(SCRIPT.parent))
import weekly_report as wr  # reused for commits()/fetch_mrs() — one source of truth with /weekly-report
BOT_DIR = pathlib.Path(__file__).resolve().parent
STATE = BOT_DIR / "state.json"
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tehran"))
POST_DOW = int(os.environ.get("POST_DOW", "3"))        # Monday=0 … Thursday=3
POST_HOUR = int(os.environ.get("POST_HOUR", "19"))
EVENING_HOUR = int(os.environ.get("EVENING_HOUR", "17"))
EVENING_MINUTE = int(os.environ.get("EVENING_MINUTE", "30"))
EVENING_DOWS = {6, 0, 1, 2, 3}  # Sun–Thu (Python weekday: Mon=0 … Sun=6)
MORNING_HOUR = int(os.environ.get("MORNING_HOUR", "10"))
MORNING_MINUTE = int(os.environ.get("MORNING_MINUTE", "0"))
QA_TIMEOUT = int(os.environ.get("QA_TIMEOUT", "240"))
WEEKLY_TIMEOUT = int(os.environ.get("WEEKLY_TIMEOUT", "2400"))
QA_TOOLS = ["Read", "Glob", "Grep", "Bash(git -C * log*)", "Bash(ls *)"]
WEEKLY_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash(python3 *)", "Bash(git *)", "Bash(glab *)", "Bash(ls *)", "mcp__atlassian__searchJiraIssuesUsingJql", "mcp__atlassian__getJiraIssue"]
# Vault-writing jobs (evening close, brag doc) get Write/Edit plus git ops scoped to the vault path only —
# never a bare "Bash(git *)" here, so a bad prompt can't touch another repo or run a destructive git command.
VAULT_WRITE_TOOLS = ["Read", "Write", "Edit", "Glob",
                     f"Bash(git -C {VAULT} status*)", f"Bash(git -C {VAULT} add*)", f"Bash(git -C {VAULT} commit*)",
                     f"Bash(git -C {VAULT} push*)", f"Bash(git -C {VAULT} diff*)", f"Bash(git -C {VAULT} log*)"]
# Calendar is read-only display, not a ground-truth-verified write path like the worklog diff —
# letting the model call the tool and report durations directly is an acceptable tier here.
CALENDAR_TOOLS = ["mcp__gcal-mcp__get_events", "mcp__gcal-mcp__list_calendars"]
GOOGLE_EMAIL = os.environ.get("GOOGLE_CALENDAR_EMAIL", "sajjad.vahedi@partnerz.io")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
run_lock = asyncio.Lock()


# ---------------------------------------------------------------- helpers
def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=1))


def last_sunday(d=None):
    d = d or dt.datetime.now(TZ).date()
    return d - dt.timedelta((d.weekday() + 1) % 7)


def is_owner(user):
    return user.id == OWNER_ID


def allowed_here(message):
    if not is_owner(message.author):
        return False
    if isinstance(message.channel, discord.DMChannel):
        return True
    return CHANNEL_ID == 0 or message.channel.id == CHANNEL_ID


async def vault_pull():
    if not (VAULT / ".git").is_dir():
        return "vault: not cloned"
    p = await asyncio.create_subprocess_exec("git", "-C", str(VAULT), "pull", "--ff-only", "-q",
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await p.communicate()
    head = subprocess.run(["git", "-C", str(VAULT), "log", "-1", "--format=%h %cd %s", "--date=format:%Y-%m-%d %H:%M"],
                          capture_output=True, text=True).stdout.strip()
    return f"vault: {'pulled' if p.returncode == 0 else 'pull failed: ' + out.decode(errors='replace')[-200:]} · {head}"


# Ground truth for what a write-mode claude run actually did — never trust its self-report alone.
# Called via asyncio.to_thread (git is fast; keeps it off the event loop without going full async).
def vault_head():
    return subprocess.run(["git", "-C", str(VAULT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def vault_diff_stat(before, after):
    return subprocess.run(["git", "-C", str(VAULT), "diff", "--stat", before, after], capture_output=True, text=True).stdout.strip()


def vault_diff_added_lines(before, after, path=None):
    cmd = ["git", "-C", str(VAULT), "diff", "--unified=0", before, after] + (["--", path] if path else [])
    out = subprocess.run(cmd, capture_output=True, text=True).stdout
    return [l[1:] for l in out.splitlines() if l.startswith("+") and not l.startswith("+++")]


def vault_pushed():
    """True only if local HEAD actually reached origin — a local commit with a still-read-only
    deploy key looks identical to a real push from vault_head() alone, so this checks upstream."""
    subprocess.run(["git", "-C", str(VAULT), "fetch", "-q"], capture_output=True)
    local = vault_head()
    up = subprocess.run(["git", "-C", str(VAULT), "rev-parse", "@{u}"], capture_output=True, text=True)
    return local == up.stdout.strip() if up.returncode == 0 else None


async def claude_run(prompt, tools, timeout, cwd, extra=()):
    cmd = [CLAUDE, "-p", prompt, "--output-format", "json", "--allowedTools", *tools, "--add-dir", str(REPORTS), str(VAULT), *extra]
    log.info("claude: %s", " ".join(shlex.quote(c) for c in cmd)[:300])
    p = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        p.kill()
        return None, f"timed out after {timeout}s"
    try:
        j = json.loads(out.decode())
        return j.get("result") or "", (None if not j.get("is_error") else "claude reported an error")
    except Exception:
        return None, (err.decode(errors="replace")[-400:] or out.decode(errors="replace")[-400:])


def chunks(text, n=1900):
    text = text or "(empty answer)"
    out, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > n:
            out.append(cur); cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out or ["(empty answer)"]


async def send_long(dest, text):
    for c in chunks(text):
        await dest.send(c)


# ---------------------------------------------------------------- Q&A
async def answer(dest, question):
    if run_lock.locked():
        await dest.send("Busy with another run — ask again in a moment.")
        return
    async with run_lock:
        async with dest.typing():
            note = await vault_pull()
            log.info(note)
            result, err = await claude_run(question, QA_TOOLS, QA_TIMEOUT, BOT_DIR)
    if err:
        await dest.send(f"Could not answer: {err}")
    else:
        await send_long(dest, result)


# ---------------------------------------------------------------- weekly
def weekly_embed(week):
    m = json.loads((REPORTS / week / "metrics.json").read_text())
    tot = sum(m["wall"].values()) / 60 or 1e-9
    share = lambda c: f"{m['wall'].get(c, 0) / 60 / tot * 100:.0f}%"
    merged = sum(1 for x in m["mrs"] if x["state"] == "merged"); opened = sum(1 for x in m["mrs"] if x["state"] == "open")
    e = discord.Embed(title=m.get("title") or f"Week of {week}", description=f"{m['week']} · {tot:.1f} active hours over {m['active_days']} working days")
    e.add_field(name="BF · merchant bugs", value=share("maintenance"), inline=True)
    e.add_field(name="AP · availability/perf", value=share("reliability"), inline=True)
    e.add_field(name="FD · features", value=share("feature"), inline=True)
    e.add_field(name="WD · workspace", value=share("workspace"), inline=True)
    e.add_field(name="MRs", value=f"{merged} merged · {opened} open", inline=True)
    e.add_field(name="Commits", value=str(m["kpis"]["commits"]), inline=True)
    subs = m.get("subjects") or []
    if subs:
        e.add_field(name="Subjects", value="\n".join(f"`{s['tag']}` {s['subject'][:70]}" for s in subs[:8]), inline=False)
    url_f = REPORTS / week / "artifact-url.txt"
    if url_f.is_file():
        e.url = url_f.read_text().strip()
    e.set_footer(text="supervised agent time only — meetings and chat are in no log")
    return e


async def run_weekly(dest, week):
    if run_lock.locked():
        await dest.send("Busy with another run — try again in a moment.")
        return
    async with run_lock:
        await dest.send(f"Generating the report for the week starting {week} — this takes a few minutes.")
        prompt = (f"Use the /weekly-report skill for the week starting {week}. Do steps 0 through 5: dry run, review sessions.json, "
                  f"write overrides.json (sessions, labels, subjects with BF/AP/FD/WD tags, day_labels), enrich jira.json through the Atlassian MCP, "
                  f"write narrative.html only for what the data supports, then build. Do NOT publish an Artifact — the bot posts the file. "
                  f"When {REPORTS}/{week}/report.html exists, reply with exactly one line: DONE.")
        async with dest.typing():
            result, err = await claude_run(prompt, WEEKLY_TOOLS, WEEKLY_TIMEOUT, BOT_DIR, extra=["--append-system-prompt", "You are running unattended from a Discord bot. Never ask questions; make the judgment calls the skill describes and state assumptions in the narrative's Method section."])
    report = REPORTS / week / "report.html"
    if not report.is_file():
        await dest.send(f"The report was not produced. {err or ''}\n{(result or '')[-800:]}")
        return
    try:
        await dest.send(embed=weekly_embed(week), file=discord.File(str(report), filename=f"weekly-{week}.html"))
    except Exception as ex:
        await dest.send(f"Report built at `{report}` but posting failed: {ex}")
    s = load_state(); s["last_posted_week"] = week; save_state(s)
    await run_brag(dest, week)
    await run_snippet(dest, week)


# ---------------------------------------------------------------- evening close
def mr_ref(m):
    return (m.get("references") or {}).get("full") or f"!{m.get('iid', '?')}"


def build_evening_prompt(day, jal, rel, commits, merged_mrs, touched_mrs, transcripts_block, review_notes, vault_reviews):
    lines = [f"Reconcile today's Obsidian worklog AND map it against what actually happened today. "
             f"Today is {day.isoformat()} = Jalali {jal} (his work week is Sunday–Thursday).",
             f"Worklog file, relative to the vault root {VAULT}: `{rel}`",
             "", "Facts already gathered for you — ground truth, do not re-derive them:", "",
             "Commits authored today:"]
    lines += [f"- [{c['cat']}] {c['repo']} {c['h']} {c['s']}" for c in commits] if commits else ["- (none)"]
    lines += ["", "MRs merged today:"]
    lines += [f"- {mr_ref(m)} {m['title']}" for m in merged_mrs] if merged_mrs else ["- (none)"]
    lines += ["", "MRs opened/updated today, not yet merged:"]
    lines += [f"- {mr_ref(m)} {m['title']} ({m['state']})" for m in touched_mrs] if touched_mrs else ["- (none)"]
    lines += ["", "His own code review activity today — real comments/approvals he wrote on colleagues' MRs "
                   "(from GitLab, not a guess):"]
    if review_notes:
        for rn in review_notes:
            kind = "approved" if rn["system"] else "commented"
            lines.append(f"- {rn['mr']} {rn['title']} — {kind}: \"{rn['body']}\"")
    else:
        lines.append("- (none)")
    if vault_reviews:
        lines.append(f"He also wrote/updated review notes in the vault's Reviews/ folder today: {', '.join(vault_reviews)}")
    lines += ["", "Condensed transcripts of today's substantial sessions — his messages and the assistant's replies, "
                   "real content, not just topic labels. This is what he actually worked on, discussed, or "
                   "investigated today, use it to know what really happened:", "", transcripts_block]
    lines += ["", (
        "Steps:\n"
        "1. Read the worklog file if it exists — this morning's planned checklist. If it doesn't exist, create it: "
        "look at a recent worklog in the same month folder for the format (a 'Main tasks' heading then a "
        "checklist), and if this week's Sunday check-in file exists, seed the list from its still-open items.\n"
        "2. Tick `- [ ]` to `- [x]` only where a commit or merged MR clearly supports it — never on a guess, "
        "and never from session activity alone.\n"
        "3. If real work happened today with no matching line: a commit/MR with no line gets appended ticked, "
        "marked `- [x] (unplanned) <what>`. Session activity with no commit and no matching line gets appended "
        "UNTICKED, marked `- [ ] (session) <what, ~Xh>`. Skip trivial or one-line sessions; only log ones with "
        "real substance.\n"
        "4. Leave every other line untouched. Never delete or reword an existing line. Never touch any file "
        "other than this one worklog.\n"
        f"5. If the file actually changed: `git -C {VAULT} add -A -- \"{rel}\"`, commit with message "
        f"`evening close: {jal} ({day.isoformat()})`. If nothing changed, skip the commit.\n"
        f"6. Always end with `git -C {VAULT} push`, whether or not you just committed — an earlier run may have "
        "committed locally without managing to push. If the push fails, say so plainly; do not claim success.\n"
        "7. Now build a TASK MAP — this is the whole point of the run, read the transcripts above properly, "
        "don't skim. For EVERY planned item in the worklog (every `- [ ]`/`- [x]` line from the ORIGINAL file, "
        "before your edits — including ones you just touched), write one line matching it against what the "
        "session transcripts show actually happened. Judge from real content, not from whether a line got "
        "ticked:\n"
        "   ✅ <task text, short> — <one clause: what actually got done>\n"
        "   🔸 <task text, short> — in progress: <one clause: what happened, what's left>\n"
        "   ➖ <task text, short> — no activity today\n"
        "   Code review is a real day activity, not a footnote — if his review comments/approvals above match a "
        "planned line (e.g. 'review X's MR'), fold it into that line's ✅/🔸 clause using what he actually wrote, "
        "not just 'reviewed it'. If no planned line covers it, it still counts as substantial work.\n"
        "   If substantial work happened that matches NO planned line — including review activity above with no "
        "matching line — add a final block (blank line before it) of 🆕 lines, same one-clause style, for that "
        "unplanned work only.\n"
        f"8. Separately, call the calendar tool ({', '.join(CALENDAR_TOOLS)}) for calendar_id=primary, "
        f"user_google_email={GOOGLE_EMAIL}, bounded to {day.isoformat()}T00:00:00+03:30 through "
        f"{(day + dt.timedelta(1)).isoformat()}T00:00:00+03:30. The tool returns times in whatever timezone each "
        "event was created with, NOT Tehran — convert every time to Asia/Tehran yourself before reporting. A REAL "
        "meeting has other attendees or is plainly a call/meeting by its title; SKIP all-day markers, 'Focus time', "
        "'Out of Office', and any personal self-block with no other attendee. An all-day event's end date is "
        "exclusive — if today falls only on that exclusive end boundary, it does not belong to today; skip it. For "
        "each real meeting: `<title> — HH:MM–HH:MM Tehran (~Xm)`. If the tool call fails or returns none, say so "
        "plainly rather than inventing a meeting.\n"
        "9. Reply with EXACTLY these two sections, in this order, nothing before the first or after the second:\n"
        "MEETINGS:\n<one line per real meeting from step 8, or 'No meetings today.' if genuinely none, or the "
        "literal tool error if the call failed>\n\nTASKMAP:\n<the task map from step 7, or if the worklog has no "
        "planned items and no substantial activity happened, exactly: No planned items and no substantial "
        "activity today.>"
    )]
    return "\n".join(lines)


EVENING_SYSTEM_PROMPT = ("You are running unattended from a Discord bot's evening-close job. Never ask questions. "
                        "Only tick a worklog item when a commit or MR clearly supports it. Only touch the one "
                        "worklog file and vault git plumbing — never any other file, never any other repo. Your "
                        "final reply must be EXACTLY the two sections (MEETINGS: then TASKMAP:) — no other text "
                        "before, between headers and content, or after.")


def session_excerpt(msgs, sid, today_iso, max_msgs=14, max_chars=900):
    """Real conversation content for one session, today's slice only, cleaned of command/wrapper
    noise — this is what lets the model judge what ACTUALLY happened, not just a topic label."""
    mine = sorted((m for m in msgs if m["sid"] == sid and m["d"] == today_iso and not wr.INJECTED(m["text"])), key=lambda m: m["t"])
    lines, total = [], 0
    for m in mine[:max_msgs]:
        text = " ".join(m["text"].split())[:220]
        if not text:
            continue
        line = f"{'You' if m['role'] == 'user' else 'Assistant'}: {text}"
        if total + len(line) > max_chars:
            break
        lines.append(line); total += len(line)
    return "\n".join(lines)


def review_wall_split(daily, msgs, today_iso, reviewer_mrs):
    """Wall-clock minutes attributable to REAL review activity, carved OUT of whichever BF/AP/FD/WD
    bucket each matching ledger row was classified into — so the chart shows review as its own line
    instead of silently inflating one of the four categories.

    Deliberately NOT a keyword match on session text (that was the first attempt, and it badly
    over-counted: this workspace's own product IS a code-review pipeline, so a session about
    building /code-review, review lenses, or 'review rounds' in the Shopify Flow pipeline is full
    of review vocabulary without him personally reviewing anyone's code — it inflated 3 of 4 test
    days to 85-100% 'review', including one at 100% with zero commits explaining it). Grounded
    instead in real GitLab reviewer-role MR numbers (`reviewer_mrs`, author-excluded, from
    fetch_reviewer_mrs — no day filter, since a real review pass on an MR doesn't always land on
    the exact calendar day GitLab stamps as its last `updated_at`): a row counts as review only if
    its session text names one of those MR refs (`!1607`) verbatim. Reviewing done purely in the
    GitLab UI with no Claude session leaves no ledger row to carve from, and correctly contributes
    zero session time here — this only redistributes minutes that already exist in the ledger."""
    refs = {f"!{m['iid']}" for m in reviewer_mrs if m.get("iid")}
    wall = dict(daily["wall"])
    if not refs:
        return wall, 0.0
    pattern = re.compile("|".join(re.escape(r) + r"\b" for r in refs))
    review_min_by_cat = collections.Counter()
    for r in daily["ledger"]:
        text = " ".join(m["text"] for sid in r["sids"] for m in msgs if m["sid"] == sid and m["d"] == today_iso)
        if pattern.search(text):
            review_min_by_cat[r["cat"]] += r["hours"] * 60
    review_min = 0.0
    for cat, mins in review_min_by_cat.items():
        take = min(mins, wall.get(cat, 0))
        wall[cat] = wall.get(cat, 0) - take
        review_min += take
    return wall, review_min


VAULT_TIME_BLOCK = re.compile(r"\(start:\s*(\d{1,2}):(\d{2})\s*,\s*end:\s*(\d{1,2}):(\d{2})\)", re.I)


def vault_worklog_review_minutes(rel):
    """Real review time he logged by hand with an explicit '(start: HH:MM, end: HH:MM)' block on a
    checked, review-worded main-task line — activity with NO Claude session behind it at all (he
    read a colleague's PR or Sentry dashboard directly), so review_wall_split can never find it:
    there is no ledger row to carve it from, and it isn't in daily['wall'] either. Confirmed from
    two real lines (1405-06-12, 1405-06-18), both 'Review <name>'s work on ...' with this exact
    annotation and no matching session — the only record of that time is the worklog itself. Adds
    genuinely new wall-clock minutes (to both the review bucket and the day's total), rather than
    redistributing existing ones."""
    p = VAULT / rel
    if not p.is_file():
        return 0.0
    total = 0.0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not re.match(r"-\s\[[xX]\]", line) or not re.search(r"\breview", line, re.I):
            continue
        m = VAULT_TIME_BLOCK.search(line)
        if m:
            sh, sm, eh, em = map(int, m.groups())
            mins = (eh * 60 + em) - (sh * 60 + sm)
            if mins > 0:
                total += mins
    return total


def fetch_reviewer_mrs(root, since_date):
    """MRs where he's a reviewer (not author) touched today — GitLab ground truth, independent of
    what any session transcript says. Mirrors wr.fetch_mrs()'s shape but a different API scope
    (reviewer_username, not scope=created_by_me), so it stays local to the daily close rather than
    widen the shared weekly_report.py module for a feature only this command uses."""
    since_utc = dt.datetime.fromisoformat(f"{since_date}T00:00:00+03:30").astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in wr.repos_under(root):
        try:
            out = subprocess.run(["glab", "api", f"merge_requests?reviewer_username={GITLAB_USER}&scope=all&updated_after={since_utc}&per_page=100"],
                                 cwd=r, capture_output=True, text=True, timeout=60).stdout
            data = json.loads(out)
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def fetch_review_notes_today(root, mrs, today_iso):
    """His own real review comments/approvals today on MRs where he's a reviewer — the actual
    content of a review, not just 'he's assigned as reviewer' (that's what fetch_reviewer_mrs
    already gives). One extra API call per MR, bounded to the MRs already identified as touched
    by his review activity that day, so this stays cheap."""
    anchors = wr.repos_under(root)
    if not anchors:
        return []
    out = []
    for m in mrs:
        pid, iid = m.get("project_id"), m.get("iid")
        if not pid or not iid:
            continue
        try:
            raw = subprocess.run(["glab", "api", f"projects/{pid}/merge_requests/{iid}/notes?per_page=100"],
                                 cwd=anchors[0], capture_output=True, text=True, timeout=30).stdout
            notes = json.loads(raw)
        except Exception:
            continue
        if not isinstance(notes, list):
            continue
        for n in notes:
            if (n.get("author") or {}).get("username") != GITLAB_USER:
                continue
            if wr.tehran_date(n.get("created_at") or "") != today_iso:
                continue
            body = (n.get("body") or "").strip()
            if body:
                out.append(dict(mr=mr_ref(m), title=m["title"], body=body[:280], system=bool(n.get("system"))))
    return out


def vault_reviews_today(today_iso):
    """Review note files under Reviews/ in the vault that he touched today, via git log — his own
    written record of a review, cross-referenced against the GitLab-side signal above."""
    out = subprocess.run(["git", "-C", str(VAULT), "log", f"--since={today_iso}T00:00:00+03:30",
                          f"--until={today_iso}T23:59:59+03:30", "--name-only", "--pretty=", "--", "Reviews/"],
                         capture_output=True, text=True).stdout
    return sorted({p.strip() for p in out.splitlines() if p.strip()})


def open_reviewer_mrs(root):
    """Full open reviewer inbox — MRs where he's reviewer, not author, still open. Unlike
    fetch_reviewer_mrs (bounded to 'touched today', for the evening close), this has no date
    window: it's the whole backlog waiting on him, for the morning brief / review radar."""
    for r in wr.repos_under(root):
        try:
            out = subprocess.run(["glab", "api", f"merge_requests?reviewer_username={GITLAB_USER}&scope=all&state=opened&per_page=100"],
                                 cwd=r, capture_output=True, text=True, timeout=60).stdout
            data = json.loads(out)
            if isinstance(data, list):
                return [m for m in data if (m.get("author") or {}).get("username") != GITLAB_USER]
        except Exception:
            continue
    return []


def mr_age_hours(m):
    created = dt.datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
    return (dt.datetime.now(dt.timezone.utc) - created).total_seconds() / 3600


def prev_workday(d):
    """Previous Sun-Thu work day, skipping the Fri/Sat weekend — 'yesterday' for a Sunday brief
    is Thursday, not Saturday."""
    p = d - dt.timedelta(days=1)
    while p.weekday() not in EVENING_DOWS:
        p -= dt.timedelta(days=1)
    return p


def worklog_checklist(rel):
    """(checked, text) for every '- [ ]'/'- [x]' line in a vault worklog file, or None if the
    file doesn't exist yet (a real, common state at 08:00 — he often writes the plan later)."""
    p = VAULT / rel
    if not p.is_file():
        return None
    items = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"-\s\[([ xX])\]\s+(.*)", line.strip())
        if m:
            items.append((m.group(1).lower() == "x", m.group(2).strip()))
    return items


def unfinished_main_tasks(items):
    """Unchecked lines that are recurring plan items — excludes the evening close's own
    '(session)'/'(unplanned)' activity-log entries. Confirmed from his own worklog history: only
    the untagged 'Main tasks' bullets get retyped at the top of the next day's file by hand; a
    day's session/unplanned log never carries forward."""
    return [t for c, t in items if not c and not re.match(r"\(session\)|\(unplanned\)", t, re.I)]


def seed_todays_worklog(rel, carried):
    """Create today's worklog file in his own 'Main tasks:' format, seeded with yesterday's
    unfinished main tasks — mirrors what he does by hand every morning. Never overwrites an
    existing file (if he already wrote today's plan, that's authoritative)."""
    p = VAULT / rel
    if p.is_file():
        return False
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["Main tasks:"] + [f"- [ ] {t}" for t in carried]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def push_worklog(rel, jal):
    subprocess.run(["git", "-C", str(VAULT), "add", "--", rel], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(VAULT), "commit", "-m", f"morning brief: seed {jal} worklog"], capture_output=True, text=True)
    subprocess.run(["git", "-C", str(VAULT), "push"], capture_output=True, text=True)


def meetings_today(month_folder, jal):
    """Meeting note titles for today, from the vault — a cross-check/fallback alongside the real
    Google Calendar durations in the MEETINGS section of the model's reply (build_evening_prompt
    step 8); shown only when Calendar found nothing, since no meeting note records duration."""
    mdir = VAULT / month_folder / "Meetings"
    if not mdir.is_dir():
        return []
    return sorted((p.stem[len(jal):].strip(" -") or p.stem) for p in mdir.rglob(f"{jal}*.md"))


def transcripts_for(daily, msgs, today_iso, cap=8):
    rows = sorted((r for r in daily["ledger"] if r["hours"] > 0.03 or r["msgs"] >= 3), key=lambda r: -r["hours"])[:cap]
    blocks = []
    for r in rows:
        text = "\n\n".join(filter(None, (session_excerpt(msgs, sid, today_iso) for sid in r["sids"])))
        if text:
            blocks.append(f"--- {r['what'][:70]} (~{r['hours']:.2g}h) ---\n{text}")
    return "\n\n".join(blocks) if blocks else "(no substantial session content today)"


def bar_ascii(pct, width=14):
    filled = max(0, min(width, round(width * pct / 100)))
    return "█" * filled + "░" * (width - filled)


def category_block(wall, tot, review_min=0):
    if tot <= 1e-6:
        return None
    lines = []
    for c in wr.CATS:
        v = wall.get(c, 0)
        if not v:
            continue
        pct = v / tot * 100
        lines.append(f"{wr.SHORT[c]:<3}{bar_ascii(pct)} {pct:>3.0f}%  {wr.h(v / 60)}")
    if review_min > 1e-6:
        pct = review_min / tot * 100
        lines.append(f"{'RV':<3}{bar_ascii(pct)} {pct:>3.0f}%  {wr.h(review_min / 60)}")
    return "\n".join(lines)


async def run_evening_close(dest, day=None):
    if run_lock.locked():
        await dest.send("Busy with another run — try again in a moment.")
        return
    async with run_lock:
        day = day or dt.datetime.now(TZ).date()
        try:
            y, m, d = jalali.greg_to_jalali(day)
        except ValueError as e:
            await dest.send(f"Can't place that date on the 1405 calendar: {e}")
            return
        jal, rel = jalali.jalali_str(m, d), jalali.file_rel(m, d)
        log.info(await vault_pull())
        # The SAME per-session/git/GitLab computation the weekly report uses, windowed to one day —
        # so "today" means real active hours and threads, not only what got committed or merged.
        wk_sunday = last_sunday(day)
        ov = wr.load_json(REPORTS / wk_sunday.isoformat() / "overrides.json")
        jira = wr.load_json(REPORTS / "jira.json")
        msgs, steps = wr.load_sessions(str(ROOT / "notify-me-workspace" / "logs"))
        since = day.isoformat()
        mrs_all = wr.fetch_mrs(str(ROOT), since)
        ctx = dict(msgs=msgs, steps=steps, root=str(ROOT), author=GIT_AUTHOR, cap=20, mrs=mrs_all)
        daily = wr.compute_week(day, 1, ctx, ov, jira, with_mrs=True)
        commits = daily["commits"]
        # daily["mrs"] is already the classified {ref,title,state,...} shape from wr.merge_requests();
        # build_evening_prompt's mr_ref() wants the raw GitLab dicts (it falls back to m['iid']), so
        # filter mrs_all directly rather than reuse daily["mrs"].
        merged = [x for x in mrs_all if wr.tehran_date(x.get("merged_at") or "") == since]
        touched = [x for x in mrs_all if x.get("state") == "opened" and wr.tehran_date(x.get("updated_at") or "") == since]
        mrs_reviewer = [x for x in await asyncio.to_thread(fetch_reviewer_mrs, str(ROOT), since)
                        if (x.get("author") or {}).get("username") != GITLAB_USER]
        reviewing = [x for x in mrs_reviewer if wr.tehran_date(x.get("updated_at") or "") == since]
        adj_wall, review_min = review_wall_split(daily, msgs, since, mrs_reviewer)
        review_notes = await asyncio.to_thread(fetch_review_notes_today, str(ROOT), reviewing, since)
        vault_reviews = await asyncio.to_thread(vault_reviews_today, since)
        vault_meetings = meetings_today(jalali.folder_name(m), jal)
        before = await asyncio.to_thread(vault_head)
        prompt = build_evening_prompt(day, jal, rel, commits, merged, touched, transcripts_for(daily, msgs, since), review_notes, vault_reviews)
        async with dest.typing():
            result, err = await claude_run(prompt, VAULT_WRITE_TOOLS + CALENDAR_TOOLS, QA_TIMEOUT, BOT_DIR, extra=["--append-system-prompt", EVENING_SYSTEM_PROMPT])
        after = await asyncio.to_thread(vault_head)
    if err:
        await dest.send(f"Evening close failed: {err}")
        return
    pushed = await asyncio.to_thread(vault_pushed)
    changed = after != before
    added = await asyncio.to_thread(vault_diff_added_lines, before, after, rel) if changed else []
    tot = sum(daily["wall"].values())
    vault_review_min = await asyncio.to_thread(vault_worklog_review_minutes, rel)
    review_min += vault_review_min
    tot += vault_review_min

    if pushed is False:
        color = discord.Color.orange()
    elif changed:
        color = discord.Color.green()
    elif tot > 1e-6:
        color = discord.Color.blurple()
    else:
        color = discord.Color.light_grey()

    desc = [f"**{day.strftime('%A, %d %B')}**"]
    if tot > 1e-6:
        line = f"{tot/60:.1f}h active · {daily['kpis']['sessions']} sessions · {daily['kpis']['commits']} commits · {len(merged)} merged / {len(touched)} open"
        desc.append(line)
        cat = category_block(adj_wall, tot, review_min)
        if cat:
            desc.append(f"```\n{cat}\n```")
    else:
        desc.append(f"No session activity logged · {daily['kpis']['commits']} commits · {len(merged)} merged / {len(touched)} open")
    e = discord.Embed(title=f"🌙 Evening Close — {jal}", description="\n".join(desc), color=color)

    # The model's reply is EXACTLY two sections (enforced by the prompt): real Calendar meetings
    # with Tehran-converted duration, then the plan-vs-actual task map. Split on the TASKMAP:
    # header rather than trust the model to keep them in separate messages.
    raw = (result or "").strip()
    if "TASKMAP:" in raw:
        meetings_part, taskmap_part = raw.split("TASKMAP:", 1)
        meetings_part = meetings_part.replace("MEETINGS:", "", 1).strip()
    else:
        meetings_part, taskmap_part = "", raw
    if meetings_part and meetings_part != "No meetings today.":
        e.add_field(name="🤝 Meetings today", value=meetings_part[:1024], inline=False)
    elif vault_meetings:
        # Calendar found nothing (or the tool call failed) but a vault note exists for today —
        # surface it rather than silently show no meetings at all.
        e.add_field(name="🤝 Meetings today (from vault notes)", value="\n".join(f"• {t}" for t in vault_meetings)[:1024], inline=False)

    # Primary content: the model's plan-vs-actual read of today's real conversations, not a
    # mechanical ledger dump. Trusted verbatim — its own instructions constrain the format tightly.
    # One line per item per the prompt, so a blank line between every non-empty line reliably
    # spaces the whole list without depending on the model to add spacing itself.
    lines = [l for l in taskmap_part.strip().splitlines() if l.strip()]
    taskmap = "\n\n".join(lines) if lines else "_(no task map produced)_"
    e.add_field(name="🗺️ Task Map", value=taskmap[:1024], inline=False)

    if daily["commits"]:
        by_repo = collections.Counter(c["repo"] for c in daily["commits"])
        val = "\n".join(f"`{repo}` × {n}" for repo, n in by_repo.most_common(6))
        e.add_field(name=f"📦 Commits ({len(daily['commits'])})", value=val[:1024], inline=True)

    if merged or touched:
        lines = [f"✅ {mr_ref(m)} {m['title'][:45]}" for m in merged[:4]] + [f"🟡 {mr_ref(m)} {m['title'][:45]}" for m in touched[:4]]
        e.add_field(name="🔀 Merge requests", value="\n".join(lines)[:1024], inline=True)

    if reviewing:
        lines = [f"👀 {mr_ref(m)} {m['title'][:45]}" for m in reviewing[:6]]
        e.add_field(name=f"🔎 Reviewing ({len(reviewing)})", value="\n".join(lines)[:1024], inline=True)

    # Secondary field: what actually got WRITTEN to the vault file, from the git-diff ground truth
    # (`added`) — never from the model's own "I ticked X" narrative. The Task Map above is the
    # comprehension-based read; this is the mechanical proof of what landed on disk.
    if not changed:
        wl = "No changes." if pushed is not False else "No changes this run — an earlier commit is still unpushed."
    else:
        checklist = []
        for l in added[:12]:
            ls = l.strip()
            if ls.startswith("- [x]"):
                checklist.append("✅ " + ls[5:].strip())
            elif ls.startswith("- [ ]"):
                checklist.append("🆕 " + ls[5:].strip())
            else:
                checklist.append(ls)
        wl = "\n".join(checklist)[:1024] or "_(see vault)_"
    e.add_field(name="📓 Worklog diff", value=wl, inline=False)

    if pushed is False:
        e.add_field(name="⚠️ Not pushed", value="Committed locally, but the push didn't reach GitHub — the vault deploy key is likely still read-only.", inline=False)

    e.set_footer(text=f"{rel} · supervised agent time")
    await dest.send(embed=e)
    s = load_state(); s["last_evening_close"] = day.isoformat(); save_state(s)


# ---------------------------------------------------------------- brag document
def build_brag_prompt(week, metrics_path):
    return (
        f"Update the brag document from the week's report at {metrics_path} (subjects, ledger, mrs) and, if it "
        f"exists, {REPORTS / week / 'narrative.html'} for numbers or quotes.\n\n"
        f"1. Read {VAULT}/Brag/1405.md if it exists. If not, create it with exactly these section headings, in "
        "this order: '## Projects', '## Collaboration & mentorship', '## Design & documentation', "
        "'## Company building', '## What you learned', '## Outside of work', '## Fuzzy work'.\n"
        f"2. Under the right section, APPEND one line per real win from the week of {week} — format "
        f"`- **{week}** <line> (ref: RS-xxxx / !mr)` — each with a number or a quote already present in the "
        "data. Never invent a number. Skip a subject with no evidence rather than pad it.\n"
        f"3. Check the file for an existing '{week}' entry first — never duplicate a win already recorded.\n"
        f"4. If you added anything: `git -C {VAULT} add -A -- Brag/1405.md`, commit `brag: week of {week}`. "
        "If nothing was added, skip the commit.\n"
        f"5. Always end with `git -C {VAULT} push`, whether or not you just committed — an earlier run may have "
        "committed locally without managing to push. If the push fails, say so plainly; do not claim success.\n"
        "6. Reply with only the new lines you added, one per line, then the push result."
    )


BRAG_SYSTEM_PROMPT = ("You are running unattended from a Discord bot's brag-document job. Never ask questions. "
                     "Every line needs a number or a quote from the data, or it is not written. Only touch "
                     "Brag/1405.md and vault git plumbing.")


async def run_brag(dest, week):
    metrics = REPORTS / week / "metrics.json"
    if not metrics.is_file():
        await dest.send(f"No report for {week} yet — run `/weekly {week}` first.")
        return
    if run_lock.locked():
        await dest.send("Busy with another run — try again in a moment.")
        return
    async with run_lock:
        log.info(await vault_pull())
        before = await asyncio.to_thread(vault_head)
        prompt = build_brag_prompt(week, metrics)
        async with dest.typing():
            result, err = await claude_run(prompt, VAULT_WRITE_TOOLS, WEEKLY_TIMEOUT, BOT_DIR, extra=["--append-system-prompt", BRAG_SYSTEM_PROMPT])
        after = await asyncio.to_thread(vault_head)
    if err:
        await dest.send(f"Brag update failed: {err}")
        return
    title = f"Brag document — week of {week}"
    pushed = await asyncio.to_thread(vault_pushed)
    if after == before:
        note = "Nothing new to add." if pushed is not False else "Nothing new this run — but an earlier commit is still unpushed."
        await dest.send(f"**{title}**\n{note}\n{(result or '').strip()[:500]}")
        if pushed is False:
            await dest.send(f"⚠️ Local HEAD is ahead of `origin` — the vault deploy key is likely still read-only. Local commit `{after[:8]}` has not reached GitHub.")
        return
    added = [l for l in await asyncio.to_thread(vault_diff_added_lines, before, after, "Brag/1405.md") if l.strip() and not l.startswith("#")]
    e = discord.Embed(title=title, description="\n".join(added)[:3900] or "(see the vault)")
    if pushed is False:
        e.color = discord.Color.orange()
        e.add_field(name="⚠️ Not pushed", value="Committed locally, but the push did not reach GitHub — the deploy key is likely still read-only.", inline=False)
    await dest.send(embed=e)


# ---------------------------------------------------------------- weekly snippet (Discord only, no vault write)
async def run_snippet(dest, week):
    metrics = REPORTS / week / "metrics.json"
    if not metrics.is_file():
        await dest.send(f"No report for {week} yet — run `/weekly {week}` first.")
        return
    narrative = REPORTS / week / "narrative.html"
    prompt = (
        f"Write a short weekly snippet in Persian for teammates and other teams, from {metrics} and "
        f"{narrative if narrative.is_file() else '(no narrative this week — use metrics only)'}.\n"
        "Exactly four short sections, 1–3 lines each: شد (shipped), آموختم (learned), "
        "بعدی (next), and گیر کردم (blocked — omit this section entirely if nothing real is blocked). "
        "No changelog dump, no number without a source in the data, no cover-page preamble. Reply with only the snippet."
    )
    if run_lock.locked():
        await dest.send("Busy with another run — try again in a moment.")
        return
    async with run_lock:
        async with dest.typing():
            result, err = await claude_run(prompt, ["Read"], QA_TIMEOUT, BOT_DIR)
    if err:
        await dest.send(f"Snippet failed: {err}")
        return
    await send_long(dest, f"**اسنیپت هفتگی — {week}**\n\n{result}")


# ---------------------------------------------------------------- morning brief
ATLASSIAN_TOOLS = ["mcp__atlassian__getAccessibleAtlassianResources", "mcp__atlassian__getJiraIssue"]

MORNING_SYSTEM_PROMPT = (
    "You are running unattended from a Discord bot's morning-brief job. Read-only: do not create, "
    "edit, comment on, or transition any Jira issue. Only an issue whose CURRENT status is exactly "
    "'Product/Tech Check-in' (case-insensitive) is actually waiting on him — that is the only status "
    "worth surfacing here. Every other status means it's already been checked and handed off "
    "(e.g. QA PASSED), or it isn't his turn yet (e.g. Pending Customer Response, To Do) — skip those "
    "entirely, do not list them even to say they're waiting on someone else. Reply with EXACTLY one "
    "short bullet list, no preamble, no markdown headers — one line per matching issue as "
    "`KEY — summary`. Report only among the keys you were given — never add others from a broader "
    "search or from memory. If none of the given keys are in that status, reply exactly: "
    "No issues waiting on you right now."
)


def build_morning_prompt(jal, day, jira_keys):
    keys_str = ", ".join(sorted(jira_keys))
    return (
        f"Morning brief for {jal} ({day.strftime('%A, %d %B')}). These Jira keys were mentioned in "
        f"my own conversations or Obsidian worklog over the last two work days: {keys_str}\n"
        "1. Call mcp__atlassian__getAccessibleAtlassianResources to get the cloudId for partnerz.atlassian.net.\n"
        "2. Call mcp__atlassian__getJiraIssue for EACH key above with that cloudId, to get its real "
        "current status, priority and summary — never guess or reuse anything from the mention itself.\n"
        "3. Report every issue found per the reply format in your system prompt."
    )


def jira_keys_from_recent(msgs, day1_iso, day2_iso, worklog_paths):
    """Jira keys mentioned in his own conversations or Obsidian worklog over the last two work
    days — grounds the morning brief's Jira section in what he's actually been working on, instead
    of a blanket 'everything assigned to me' JQL query that surfaces stale backlog tickets he
    hasn't touched in weeks."""
    keys = set()
    norm = lambda k: "RS-" + re.sub(r"\D", "", k)
    for m in msgs:
        if m["d"] in (day1_iso, day2_iso):
            keys.update(norm(k) for k in wr.KEY.findall(m["text"]))
    for p in worklog_paths:
        fp = VAULT / p
        if fp.is_file():
            keys.update(norm(k) for k in wr.KEY.findall(fp.read_text(encoding="utf-8")))
    return keys


async def run_morning_brief(dest, day=None):
    if run_lock.locked():
        await dest.send("Busy with another run — try again in a moment.")
        return
    async with run_lock:
        is_live = day is None
        day = day or dt.datetime.now(TZ).date()
        try:
            y, m, d = jalali.greg_to_jalali(day)
        except ValueError as e:
            await dest.send(f"Can't place that date on the 1405 calendar: {e}")
            return
        jal, rel = jalali.jalali_str(m, d), jalali.file_rel(m, d)
        log.info(await vault_pull())

        py, pm, pd = jalali.greg_to_jalali(prev_workday(day))
        carried = unfinished_main_tasks(worklog_checklist(jalali.file_rel(pm, pd)) or [])

        # Only auto-create today's page on the real scheduled/default run, on a real work day —
        # never for a manual /brief <past-date> lookup, which would otherwise write a phantom
        # worklog into vault history for a day that never had one.
        seeded, pushed = False, None
        if is_live and day.weekday() in EVENING_DOWS:
            seeded = await asyncio.to_thread(seed_todays_worklog, rel, carried)
            if seeded:
                await asyncio.to_thread(push_worklog, rel, jal)
                pushed = await asyncio.to_thread(vault_pushed)

        today_items = worklog_checklist(rel)

        # A queue past a week old is almost never still "waiting on you" in any useful sense —
        # it's stale backlog, and it drowned out the real recent items (observed: MRs 200+ days
        # old sorting to the top by age).
        reviewing = await asyncio.to_thread(open_reviewer_mrs, str(ROOT))
        reviewing = [mr for mr in reviewing if mr_age_hours(mr) <= 24 * 7]
        reviewing.sort(key=mr_age_hours, reverse=True)

        d2 = prev_workday(prev_workday(day))
        _, pm2, pd2 = jalali.greg_to_jalali(d2)
        msgs, _steps = wr.load_sessions(str(ROOT / "notify-me-workspace" / "logs"))
        jira_keys = jira_keys_from_recent(msgs, prev_workday(day).isoformat(), d2.isoformat(),
                                           [jalali.file_rel(pm, pd), jalali.file_rel(pm2, pd2)])
        if jira_keys:
            prompt = build_morning_prompt(jal, day, jira_keys)
            async with dest.typing():
                jira_text, err = await claude_run(prompt, ATLASSIAN_TOOLS, QA_TIMEOUT, BOT_DIR,
                                                   extra=["--append-system-prompt", MORNING_SYSTEM_PROMPT])
        else:
            jira_text, err = "No Jira issues mentioned in the last two days.", None

    e = discord.Embed(title=f"☀️ Morning Brief — {jal}", description=f"**{day.strftime('%A, %d %B')}**", color=discord.Color.gold())

    if seeded:
        body = "\n".join(f"• {t}" for t in carried) if carried else "_(nothing carried — clean slate)_"
        e.add_field(name="🗒️ Today's plan (seeded from yesterday)", value=body[:1024], inline=False)
        if pushed is False:
            e.add_field(name="⚠️ Not pushed", value="Seeded locally, but the push didn't reach GitHub.", inline=False)
    else:
        if today_items is None:
            plan_val = "_(not written yet)_"
        else:
            unchecked = [t for c, t in today_items if not c]
            plan_val = "\n".join(f"• {t}" for t in unchecked[:10]) if unchecked else "_(nothing open)_"
        e.add_field(name="🗒️ Today's plan", value=plan_val[:1024], inline=False)
        if carried:
            e.add_field(name="⏮️ Carried from yesterday", value="\n".join(f"• {t}" for t in carried)[:1024], inline=False)

    if reviewing:
        lines = []
        for mr in reviewing[:8]:
            age = mr_age_hours(mr)
            lines.append(f"{'🔴' if age > 24 else '🟡'} {mr_ref(mr)} {mr['title'][:45]} · {age:.0f}h")
        e.add_field(name=f"🔎 Awaiting your review ({len(reviewing)})", value="\n".join(lines)[:1024], inline=False)

    e.add_field(name="📋 Jira", value=(f"_(lookup failed: {err})_" if err else (jira_text or "").strip()[:1024] or "_(none)_"), inline=False)

    await dest.send(embed=e)
    s = load_state(); s["last_morning_brief"] = day.isoformat(); save_state(s)


# ---------------------------------------------------------------- commands
@tree.command(name="weekly", description="Generate and post the weekly review (default: current week)")
@app_commands.describe(week_start="Sunday as YYYY-MM-DD (optional)")
async def weekly_cmd(inter: discord.Interaction, week_start: str = ""):
    if not is_owner(inter.user):
        await inter.response.send_message("Owner only.", ephemeral=True); return
    week = week_start or last_sunday().isoformat()
    try:
        if dt.date.fromisoformat(week).weekday() != 6:
            await inter.response.send_message("week_start must be a Sunday.", ephemeral=True); return
    except ValueError:
        await inter.response.send_message("Use YYYY-MM-DD.", ephemeral=True); return
    await inter.response.send_message(f"On it — week {week}.", ephemeral=True)
    await run_weekly(inter.channel, week)


@tree.command(name="ask", description="Ask about your week, subjects, or daily plans")
async def ask_cmd(inter: discord.Interaction, question: str):
    if not is_owner(inter.user):
        await inter.response.send_message("Owner only.", ephemeral=True); return
    await inter.response.send_message(f"> {question[:180]}", ephemeral=False)
    await answer(inter.channel, question)


@tree.command(name="whoami", description="Show your user ID and this channel's ID (for the bot's .env)")
async def whoami_cmd(inter: discord.Interaction):
    ch = inter.channel
    where = "DM" if isinstance(ch, discord.DMChannel) else f"#{getattr(ch, 'name', '?')}"
    await inter.response.send_message(f"OWNER_ID={inter.user.id}\nCHANNEL_ID={ch.id}  ({where})\nowner configured: {'yes' if OWNER_ID else 'no — put these in ~/.config/weekly-bot/.env and restart'}", ephemeral=True)


@tree.command(name="status", description="Bot status: vault, last report, schedule")
async def status_cmd(inter: discord.Interaction):
    if not is_owner(inter.user):
        await inter.response.send_message("Owner only.", ephemeral=True); return
    s = load_state(); note = await vault_pull()
    weeks = sorted(p.name for p in REPORTS.iterdir() if p.is_dir() and (p / "metrics.json").is_file()) if REPORTS.is_dir() else []
    await inter.response.send_message(f"{note}\nreports: {', '.join(weeks[-4:]) or 'none'}\n"
                                      f"last morning brief: {s.get('last_morning_brief', 'never')}\n"
                                      f"last evening close: {s.get('last_evening_close', 'never')}\nlast weekly post: {s.get('last_posted_week', 'never')}\n"
                                      f"schedule: morning brief Sun–Thu {MORNING_HOUR:02d}:{MORNING_MINUTE:02d} · evening close Sun–Thu {EVENING_HOUR:02d}:{EVENING_MINUTE:02d} · "
                                      f"weekly {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][POST_DOW]} {POST_HOUR:02d}:00 · {TZ.key}\n"
                                      f"busy: {run_lock.locked()}", ephemeral=True)


@tree.command(name="close", description="Manually run the evening close (default: today)")
@app_commands.describe(date="YYYY-MM-DD, optional")
async def close_cmd(inter: discord.Interaction, date: str = ""):
    if not is_owner(inter.user):
        await inter.response.send_message("Owner only.", ephemeral=True); return
    try:
        day = dt.date.fromisoformat(date) if date else None
    except ValueError:
        await inter.response.send_message("Use YYYY-MM-DD.", ephemeral=True); return
    await inter.response.send_message("On it.", ephemeral=True)
    await run_evening_close(inter.channel, day)


@tree.command(name="brief", description="Manually run the morning brief (default: today)")
@app_commands.describe(date="YYYY-MM-DD, optional")
async def brief_cmd(inter: discord.Interaction, date: str = ""):
    if not is_owner(inter.user):
        await inter.response.send_message("Owner only.", ephemeral=True); return
    try:
        day = dt.date.fromisoformat(date) if date else None
    except ValueError:
        await inter.response.send_message("Use YYYY-MM-DD.", ephemeral=True); return
    await inter.response.send_message("On it.", ephemeral=True)
    await run_morning_brief(inter.channel, day)


@tree.command(name="brag", description="Update the brag document from a week's report (default: current week)")
@app_commands.describe(week_start="Sunday as YYYY-MM-DD, optional")
async def brag_cmd(inter: discord.Interaction, week_start: str = ""):
    if not is_owner(inter.user):
        await inter.response.send_message("Owner only.", ephemeral=True); return
    week = week_start or last_sunday().isoformat()
    await inter.response.send_message("On it.", ephemeral=True)
    await run_brag(inter.channel, week)


@tree.command(name="snippet", description="Post the weekly snippet for a week (default: current week)")
@app_commands.describe(week_start="Sunday as YYYY-MM-DD, optional")
async def snippet_cmd(inter: discord.Interaction, week_start: str = ""):
    if not is_owner(inter.user):
        await inter.response.send_message("Owner only.", ephemeral=True); return
    week = week_start or last_sunday().isoformat()
    await inter.response.send_message("On it.", ephemeral=True)
    await run_snippet(inter.channel, week)


@client.event
async def on_message(message):
    if message.author.bot or not allowed_here(message):
        return
    text = message.content.strip()
    if not text or text.startswith("/"):
        return
    await answer(message.channel, text)


# ---------------------------------------------------------------- schedule
async def run_scheduled(coro, label):
    """A failure inside a scheduled job must never kill this loop — an uncaught exception here
    (a GitLab hiccup, a malformed session log) would silently stop every future scheduled run
    until someone notices the bot's gone quiet and manually restarts the service."""
    try:
        await coro
    except Exception:
        log.exception("scheduled %s failed", label)
        try:
            owner = client.get_user(OWNER_ID) or await client.fetch_user(OWNER_ID)
            await owner.send(f"⚠️ Scheduled {label} failed — see `journalctl --user -u weekly-bot`. The scheduler is still running.")
        except Exception:
            log.exception("could not DM owner about the %s failure", label)


async def scheduler():
    await client.wait_until_ready()
    while not client.is_closed():
        now = dt.datetime.now(TZ)
        if CHANNEL_ID and not run_lock.locked():
            s = load_state()
            if now.weekday() in EVENING_DOWS and now.hour == MORNING_HOUR and now.minute == MORNING_MINUTE \
               and s.get("last_morning_brief") != now.date().isoformat():
                ch = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
                log.info("scheduled morning brief for %s", now.date())
                await run_scheduled(run_morning_brief(ch), "morning brief")
            if now.weekday() in EVENING_DOWS and now.hour == EVENING_HOUR and now.minute == EVENING_MINUTE \
               and s.get("last_evening_close") != now.date().isoformat():
                ch = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
                log.info("scheduled evening close for %s", now.date())
                await run_scheduled(run_evening_close(ch), "evening close")
            week = last_sunday(now.date()).isoformat()
            if now.weekday() == POST_DOW and now.hour == POST_HOUR and s.get("last_posted_week") != week:
                ch = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
                log.info("scheduled weekly run for %s", week)
                await run_scheduled(run_weekly(ch, week), "weekly review")
        await asyncio.sleep(60)


@client.event
async def on_ready():
    await tree.sync()
    log.info("ready as %s · owner %s · channel %s · reports %s · vault %s", client.user, OWNER_ID, CHANNEL_ID or "DM/any", REPORTS, VAULT)
    client.loop.create_task(scheduler())


client.run(TOKEN, log_handler=None)
