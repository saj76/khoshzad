#!/usr/bin/env python3
"""Personal Discord bot: weekly engineering review + Q&A over the reports and the Obsidian vault.

Owner-only. Every answer is a headless `claude -p` run on the owner's own subscription, with
~/weekly-reports and ~/obsidian-vault added as readable dirs. The weekly report is produced by
the /weekly-report skill (steps 0-5) and posted here as an embed + the report.html file.

Config: ~/.config/weekly-bot/.env (see README). State: ~/weekly-bot/state.json.
"""
import asyncio, datetime as dt, json, logging, os, pathlib, shlex, subprocess, sys
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
QA_TIMEOUT = int(os.environ.get("QA_TIMEOUT", "240"))
WEEKLY_TIMEOUT = int(os.environ.get("WEEKLY_TIMEOUT", "2400"))
QA_TOOLS = ["Read", "Glob", "Grep", "Bash(git -C * log*)", "Bash(ls *)"]
WEEKLY_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash(python3 *)", "Bash(git *)", "Bash(glab *)", "Bash(ls *)", "mcp__atlassian__searchJiraIssuesUsingJql", "mcp__atlassian__getJiraIssue"]
# Vault-writing jobs (evening close, brag doc) get Write/Edit plus git ops scoped to the vault path only —
# never a bare "Bash(git *)" here, so a bad prompt can't touch another repo or run a destructive git command.
VAULT_WRITE_TOOLS = ["Read", "Write", "Edit", "Glob",
                     f"Bash(git -C {VAULT} status*)", f"Bash(git -C {VAULT} add*)", f"Bash(git -C {VAULT} commit*)",
                     f"Bash(git -C {VAULT} push*)", f"Bash(git -C {VAULT} diff*)", f"Bash(git -C {VAULT} log*)"]

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


def build_evening_prompt(day, jal, rel, commits, merged_mrs, touched_mrs):
    lines = [f"Reconcile today's Obsidian worklog. Today is {day.isoformat()} = Jalali {jal} "
             f"(his work week is Sunday–Thursday).",
             f"Worklog file, relative to the vault root {VAULT}: `{rel}`",
             "", "Facts already gathered for you — ground truth, do not re-derive them:", "",
             "Commits authored today:"]
    lines += [f"- [{c['cat']}] {c['repo']} {c['h']} {c['s']}" for c in commits] if commits else ["- (none)"]
    lines += ["", "MRs merged today:"]
    lines += [f"- {mr_ref(m)} {m['title']}" for m in merged_mrs] if merged_mrs else ["- (none)"]
    lines += ["", "MRs opened/updated today, not yet merged:"]
    lines += [f"- {mr_ref(m)} {m['title']} ({m['state']})" for m in touched_mrs] if touched_mrs else ["- (none)"]
    lines += ["", (
        "Steps:\n"
        "1. Read the worklog file if it exists. If not, create it: look at a recent worklog in the same month "
        "folder for the format (a 'Main tasks' heading then a checklist), and if this week's Sunday check-in "
        "file exists, seed the list from its still-open items.\n"
        "2. Tick `- [ ]` to `- [x]` only where a commit or merged MR clearly supports it — never on a guess.\n"
        "3. If real work happened today with no matching line, append it as a new item, ticked, marked "
        "distinctly: `- [x] (unplanned) <what>`.\n"
        "4. Leave every other line untouched. Never delete or reword an existing line. Never touch any file "
        "other than this one worklog.\n"
        f"5. If the file actually changed: `git -C {VAULT} add -A -- \"{rel}\"`, commit with message "
        f"`evening close: {jal} ({day.isoformat()})`. If nothing changed, skip the commit.\n"
        f"6. Always end with `git -C {VAULT} push`, whether or not you just committed — an earlier run may have "
        "committed locally without managing to push. If the push fails, say so plainly; do not claim success.\n"
        "7. Reply with a short recap: what was ticked, what was added, what stayed open, and the push result."
    )]
    return "\n".join(lines)


EVENING_SYSTEM_PROMPT = ("You are running unattended from a Discord bot's evening-close job. Never ask questions. "
                        "Only tick an item when a commit or MR clearly supports it. Only touch the one worklog "
                        "file and vault git plumbing — never any other file, never any other repo.")


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
        since = day.isoformat(); until = (day + dt.timedelta(1)).isoformat()
        commits = wr.commits(str(ROOT), GIT_AUTHOR, since, until, wr.DEFAULT_COMMIT_RULES)
        mrs_all = wr.fetch_mrs(str(ROOT), since)
        # GitLab timestamps are UTC; wr.tehran_date() converts before comparing — a raw [:10] slice
        # reads the UTC calendar day, which can be up to 3.5h off from his Tehran "today".
        merged = [x for x in mrs_all if wr.tehran_date(x.get("merged_at") or "") == since]
        touched = [x for x in mrs_all if x.get("state") == "opened" and wr.tehran_date(x.get("updated_at") or "") == since]
        before = await asyncio.to_thread(vault_head)
        prompt = build_evening_prompt(day, jal, rel, commits, merged, touched)
        async with dest.typing():
            result, err = await claude_run(prompt, VAULT_WRITE_TOOLS, QA_TIMEOUT, BOT_DIR, extra=["--append-system-prompt", EVENING_SYSTEM_PROMPT])
        after = await asyncio.to_thread(vault_head)
    if err:
        await dest.send(f"Evening close failed: {err}")
        return
    title = f"Evening close — {jal} ({day.strftime('%a %d %b')})"
    pushed = await asyncio.to_thread(vault_pushed)
    if after == before:
        note = "No changes." if pushed is not False else "No changes this run — but an earlier commit is still unpushed (see below)."
        await dest.send(f"**{title}**\n{note}\n{(result or '').strip()[:500]}")
        if pushed is False:
            await dest.send(f"⚠️ Local HEAD is ahead of `origin` — the vault deploy key is likely still read-only. Local commit `{after[:8]}` has not reached GitHub.")
        return
    stat = await asyncio.to_thread(vault_diff_stat, before, after)
    added = await asyncio.to_thread(vault_diff_added_lines, before, after, rel)
    e = discord.Embed(title=title)
    e.add_field(name="File", value=f"`{rel}`", inline=False)
    if stat:
        e.add_field(name="Diff", value=f"```\n{stat[:900]}\n```", inline=False)
    if added:
        e.add_field(name="Ticked / added", value=f"```diff\n{chr(10).join(added[:25])[:900]}\n```", inline=False)
    if pushed is False:
        e.color = discord.Color.orange()
        e.add_field(name="⚠️ Not pushed", value="Committed locally, but the push did not reach GitHub — the deploy key is likely still read-only.", inline=False)
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
                                      f"last evening close: {s.get('last_evening_close', 'never')}\nlast weekly post: {s.get('last_posted_week', 'never')}\n"
                                      f"schedule: evening close Sun–Thu {EVENING_HOUR:02d}:{EVENING_MINUTE:02d} · weekly {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][POST_DOW]} {POST_HOUR:02d}:00 · {TZ.key}\n"
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
async def scheduler():
    await client.wait_until_ready()
    while not client.is_closed():
        now = dt.datetime.now(TZ)
        if CHANNEL_ID and not run_lock.locked():
            s = load_state()
            if now.weekday() in EVENING_DOWS and now.hour == EVENING_HOUR and now.minute == EVENING_MINUTE \
               and s.get("last_evening_close") != now.date().isoformat():
                ch = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
                log.info("scheduled evening close for %s", now.date())
                await run_evening_close(ch)
            week = last_sunday(now.date()).isoformat()
            if now.weekday() == POST_DOW and now.hour == POST_HOUR and s.get("last_posted_week") != week:
                ch = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
                log.info("scheduled weekly run for %s", week)
                await run_weekly(ch, week)
        await asyncio.sleep(60)


@client.event
async def on_ready():
    await tree.sync()
    log.info("ready as %s · owner %s · channel %s · reports %s · vault %s", client.user, OWNER_ID, CHANNEL_ID or "DM/any", REPORTS, VAULT)
    client.loop.create_task(scheduler())


client.run(TOKEN, log_handler=None)
