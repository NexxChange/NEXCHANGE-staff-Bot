"""
╔══════════════════════════════════════════════════════════════╗
║          STAFF MANAGEMENT BOT  —  Full Edition              ║
║  • Exchange tracking  (daily / weekly / monthly / all-time)  ║
║  • Exchange worth & commission calculator                    ║
║  • Staff of the Week (votes + party announcement)           ║
║  • Points system (auto-earned from exchanges)               ║
║  • Commission payouts                                        ║
║  • Warnings system                                           ║
║  • Achievements / Badges                                     ║
║  • Attendance check-in / check-out                          ║
║  • Staff of the Month (auto from weekly wins)               ║
╚══════════════════════════════════════════════════════════════╝
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import json, os
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# ──────────────────────────────────────────────────────────────
#  CONFIG  — edit before running
# ──────────────────────────────────────────────────────────────
TOKEN            = "YOUR_BOT_TOKEN_HERE"
GUILD_ID         = 123456789012345678       # Your server ID
LOG_CHANNEL_ID   = None                     # Audit log channel ID (or None)
SOTW_CHANNEL_ID  = None                     # SotW announcement channel ID (or None = auto-find)
ADMIN_ROLE_NAME  = "Admin"
STAFF_ROLE_NAME  = "Staff"

# Commission & exchange worth settings
EXCHANGE_WORTH          = 50.0    # How much (in currency) one approved exchange is worth
COMMISSION_RATE         = 0.10    # 10% commission on total exchange worth per staff member
POINTS_PER_EXCHANGE     = 5       # Points awarded per approved exchange (both parties)
POINTS_SOTW_WIN         = 25      # Points for winning Staff of the Week
POINTS_SOTW_RUNNER_UP   = 10      # Points for 2nd place
CURRENCY_SYMBOL         = "Rs."   # Change to $ or whatever fits

DATA_FILE = "staff_data.json"

# ──────────────────────────────────────────────────────────────
#  DATA LAYER
# ──────────────────────────────────────────────────────────────
def default_data():
    return {
        "exchanges": [],
        "sotw_votes": {},
        "sotw_nominations": {},
        "sotw_history": [],
        "sotw_month_history": [],
        "points": {},
        "commissions": {},
        "warnings": {},
        "attendance": {},
        "achievements": {},
    }

def load_data() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        for k, v in default_data().items():
            data.setdefault(k, v)
        return data
    return default_data()

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

# ──────────────────────────────────────────────────────────────
#  BOT SETUP
# ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ──────────────────────────────────────────────────────────────
#  UTILITIES
# ──────────────────────────────────────────────────────────────
def is_admin(i: discord.Interaction) -> bool:
    return any(r.name == ADMIN_ROLE_NAME for r in i.user.roles)

def is_staff(i: discord.Interaction) -> bool:
    return any(r.name in [STAFF_ROLE_NAME, ADMIN_ROLE_NAME] for r in i.user.roles)

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M UTC")

async def audit_log(msg: str):
    if LOG_CHANNEL_ID:
        ch = bot.get_channel(LOG_CHANNEL_ID)
        if ch:
            await ch.send(f"LOG | {fmt_dt(now_utc())} — {msg}")

def week_key(dt: datetime = None) -> str:
    return (dt or now_utc()).strftime("%Y-W%W")

def month_key(dt: datetime = None) -> str:
    return (dt or now_utc()).strftime("%Y-%m")

def day_key(dt: datetime = None) -> str:
    return (dt or now_utc()).strftime("%Y-%m-%d")

def exchanges_for_user(data: dict, uid: int, status: str = "approved") -> list:
    return [
        e for e in data["exchanges"]
        if (e["requester_id"] == uid or e["target_id"] == uid)
        and (status == "all" or e["status"] == status)
    ]

def filter_by_period(exchanges: list, period: str) -> list:
    now = now_utc()
    cutoffs = {"day": now - timedelta(days=1), "week": now - timedelta(weeks=1), "month": now - timedelta(days=30)}
    if period not in cutoffs:
        return exchanges
    cutoff = cutoffs[period]
    return [
        e for e in exchanges
        if datetime.strptime(e["created_at"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc) >= cutoff
    ]

def calc_worth(n: int) -> float:
    return n * EXCHANGE_WORTH

def calc_commission(worth: float) -> float:
    return worth * COMMISSION_RATE

def exchange_embed(ex: dict, idx: int) -> discord.Embed:
    colors = {"pending": 0xFFA500, "approved": 0x2ECC71, "denied": 0xE74C3C}
    e = discord.Embed(title=f"Exchange Request #{idx}", color=colors.get(ex["status"], 0x95A5A6))
    e.add_field(name="Requester", value=f"<@{ex['requester_id']}>", inline=True)
    e.add_field(name="Target",    value=f"<@{ex['target_id']}>",    inline=True)
    e.add_field(name="Worth",     value=f"{CURRENCY_SYMBOL}{EXCHANGE_WORTH:.0f}", inline=True)
    e.add_field(name="Reason",    value=ex["reason"],               inline=False)
    e.add_field(name="Status",    value=ex["status"].upper(),       inline=True)
    e.add_field(name="Submitted", value=ex["created_at"],           inline=True)
    if ex.get("resolved_at"):
        e.add_field(name="Resolved", value=ex["resolved_at"], inline=True)
    return e

# ── ACHIEVEMENTS ─────────────────────────────────────────────
ACHIEVEMENTS = {
    "First Exchange":   lambda s: s["exchanges"] >= 1,
    "Exchange Veteran": lambda s: s["exchanges"] >= 10,
    "Exchange Master":  lambda s: s["exchanges"] >= 50,
    "SotW Champion":    lambda s: s["sotw_wins"] >= 1,
    "SotW Legend":      lambda s: s["sotw_wins"] >= 3,
    "Point Collector":  lambda s: s["points"] >= 100,
    "High Earner":      lambda s: s["commission"] >= 500,
}
ACHIEVEMENT_EMOJI = {
    "First Exchange": "🔄", "Exchange Veteran": "🏃", "Exchange Master": "🔥",
    "SotW Champion": "⭐", "SotW Legend": "👑", "Point Collector": "💎", "High Earner": "💰",
}

def check_achievements(data: dict, uid: int) -> list:
    uid_str  = str(uid)
    current  = set(data["achievements"].get(uid_str, []))
    exs      = exchanges_for_user(data, uid, "approved")
    sotw_w   = sum(1 for h in data["sotw_history"] if h["winner_id"] == uid)
    stats    = {
        "exchanges":  len(exs),
        "sotw_wins":  sotw_w,
        "points":     data["points"].get(uid_str, 0),
        "commission": data["commissions"].get(uid_str, 0.0),
    }
    new = [n for n, fn in ACHIEVEMENTS.items() if n not in current and fn(stats)]
    data["achievements"][uid_str] = list(current | set(new))
    return new

async def notify_badges(guild: discord.Guild, uid: int, badges: list):
    if not badges or not guild:
        return
    m = guild.get_member(uid)
    if not m:
        return
    lines = "\n".join(f"{ACHIEVEMENT_EMOJI.get(b, '🏅')} **{b}**" for b in badges)
    try:
        await m.send(f"You earned new achievement(s)!\n{lines}")
    except Exception:
        pass

# ──────────────────────────────────────────────────────────────
#  STARTUP
# ──────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Sync failed: {e}")
    weekly_sotw_reminder.start()
    weekly_exchange_summary.start()
    monthly_sotm_reminder.start()

# ══════════════════════════════════════════════════════════════
#  EXCHANGE COMMANDS
# ══════════════════════════════════════════════════════════════

@tree.command(name="exchange_request", description="Request a staff shift/role exchange.")
@app_commands.describe(target="Staff member to exchange with", reason="Why you need this exchange")
async def exchange_request(i: discord.Interaction, target: discord.Member, reason: str):
    if not is_staff(i):
        return await i.response.send_message("Staff only.", ephemeral=True)
    if target.id == i.user.id:
        return await i.response.send_message("Cannot exchange with yourself.", ephemeral=True)
    data = load_data()
    ex = {
        "requester_id": i.user.id,
        "target_id":    target.id,
        "reason":       reason,
        "status":       "pending",
        "created_at":   fmt_dt(now_utc()),
        "resolved_at":  None,
    }
    data["exchanges"].append(ex)
    save_data(data)
    idx = len(data["exchanges"])
    embed = exchange_embed(ex, idx)
    embed.set_footer(text=f"Admins: /exchange_approve {idx}  or  /exchange_deny {idx}")
    await i.response.send_message(embed=embed)
    await audit_log(f"Exchange #{idx} requested by {i.user} with {target}")
    try:
        await target.send(
            f"**{i.user.display_name}** has requested a staff exchange with you.\n"
            f"Reason: {reason}\nAn admin will review this soon."
        )
    except Exception:
        pass


@tree.command(name="exchange_approve", description="[Admin] Approve an exchange request.")
@app_commands.describe(exchange_id="Exchange request ID")
async def exchange_approve(i: discord.Interaction, exchange_id: int):
    if not is_admin(i):
        return await i.response.send_message("Admins only.", ephemeral=True)
    data = load_data()
    idx  = exchange_id - 1
    if idx < 0 or idx >= len(data["exchanges"]):
        return await i.response.send_message("Invalid ID.", ephemeral=True)
    ex = data["exchanges"][idx]
    if ex["status"] != "pending":
        return await i.response.send_message(f"Already {ex['status']}.", ephemeral=True)

    ex["status"]      = "approved"
    ex["resolved_at"] = fmt_dt(now_utc())

    for uid in [ex["requester_id"], ex["target_id"]]:
        k = str(uid)
        data["points"][k]      = data["points"].get(k, 0) + POINTS_PER_EXCHANGE
        data["commissions"][k] = data["commissions"].get(k, 0.0) + calc_commission(EXCHANGE_WORTH)

    save_data(data)

    for uid in [ex["requester_id"], ex["target_id"]]:
        badges = check_achievements(data, uid)
        save_data(data)
        await notify_badges(i.guild, uid, badges)

    embed = exchange_embed(ex, exchange_id)
    embed.add_field(name="Points Awarded",    value=f"+{POINTS_PER_EXCHANGE} pts each", inline=True)
    embed.add_field(name="Commission Earned", value=f"+{CURRENCY_SYMBOL}{calc_commission(EXCHANGE_WORTH):.2f} each", inline=True)
    await i.response.send_message(f"Exchange #{exchange_id} approved!", embed=embed)
    await audit_log(f"Exchange #{exchange_id} approved by {i.user}")

    for uid in [ex["requester_id"], ex["target_id"]]:
        m = i.guild.get_member(uid)
        if m:
            try:
                await m.send(
                    f"Exchange #{exchange_id} approved by {i.user.display_name}!\n"
                    f"You earned +{POINTS_PER_EXCHANGE} pts and "
                    f"+{CURRENCY_SYMBOL}{calc_commission(EXCHANGE_WORTH):.2f} commission."
                )
            except Exception:
                pass


@tree.command(name="exchange_deny", description="[Admin] Deny an exchange request.")
@app_commands.describe(exchange_id="Exchange request ID", reason="Reason for denial")
async def exchange_deny(i: discord.Interaction, exchange_id: int, reason: str = "No reason provided"):
    if not is_admin(i):
        return await i.response.send_message("Admins only.", ephemeral=True)
    data = load_data()
    idx  = exchange_id - 1
    if idx < 0 or idx >= len(data["exchanges"]):
        return await i.response.send_message("Invalid ID.", ephemeral=True)
    ex = data["exchanges"][idx]
    if ex["status"] != "pending":
        return await i.response.send_message(f"Already {ex['status']}.", ephemeral=True)
    ex["status"]      = "denied"
    ex["deny_reason"] = reason
    ex["resolved_at"] = fmt_dt(now_utc())
    save_data(data)
    embed = exchange_embed(ex, exchange_id)
    embed.add_field(name="Denial Reason", value=reason, inline=False)
    await i.response.send_message(f"Exchange #{exchange_id} denied.", embed=embed)
    await audit_log(f"Exchange #{exchange_id} denied by {i.user} | {reason}")


@tree.command(name="exchange_list", description="List exchange requests.")
@app_commands.describe(status="pending / approved / denied / all")
async def exchange_list(i: discord.Interaction, status: str = "all"):
    if not is_staff(i):
        return await i.response.send_message("Staff only.", ephemeral=True)
    data = load_data()
    exs  = [e for e in data["exchanges"] if status == "all" or e["status"] == status]
    if not exs:
        return await i.response.send_message(f"No exchanges for status: {status}", ephemeral=True)
    embed = discord.Embed(title="Staff Exchange Requests", color=0x5865F2)
    for ex in exs[-10:]:
        real_idx = data["exchanges"].index(ex) + 1
        embed.add_field(
            name=f"#{real_idx} — {ex['status'].upper()}",
            value=f"<@{ex['requester_id']}> with <@{ex['target_id']}>\n_{ex['reason']}_",
            inline=False
        )
    embed.set_footer(text="Showing last 10")
    await i.response.send_message(embed=embed)

# ──────────────────────────────────────────────────────────────
#  EXCHANGE STATS & WORTH
# ──────────────────────────────────────────────────────────────

@tree.command(name="exchange_stats", description="View exchange stats and worth (day/week/month/alltime).")
@app_commands.describe(member="Leave blank for yourself", period="day / week / month / alltime")
async def exchange_stats(i: discord.Interaction, member: discord.Member = None, period: str = "week"):
    member = member or i.user
    period = period.lower()
    if period not in ("day", "week", "month", "alltime"):
        return await i.response.send_message("Period must be: day / week / month / alltime", ephemeral=True)

    data         = load_data()
    all_approved = exchanges_for_user(data, member.id, "approved")
    all_denied   = exchanges_for_user(data, member.id, "denied")
    all_pending  = exchanges_for_user(data, member.id, "pending")
    period_exs   = filter_by_period(all_approved, period)

    worth      = calc_worth(len(period_exs))
    commission = calc_commission(worth)
    total_pts  = data["points"].get(str(member.id), 0)
    total_comm = data["commissions"].get(str(member.id), 0.0)
    sotw_wins  = sum(1 for h in data["sotw_history"] if h["winner_id"] == member.id)
    badges     = data["achievements"].get(str(member.id), [])

    label = {"day": "Today", "week": "This Week", "month": "This Month", "alltime": "All Time"}[period]

    embed = discord.Embed(
        title=f"Exchange Stats — {member.display_name}",
        description=f"Period: **{label}**",
        color=0x5865F2
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Approved Exchanges", value=str(len(period_exs)),                   inline=True)
    embed.add_field(name="Exchange Worth",     value=f"{CURRENCY_SYMBOL}{worth:,.2f}",       inline=True)
    embed.add_field(name="Commission Earned",  value=f"{CURRENCY_SYMBOL}{commission:,.2f}", inline=True)

    embed.add_field(name="\u200b", value="── All-Time ──", inline=False)
    embed.add_field(name="Total Approved",       value=str(len(all_approved)),                          inline=True)
    embed.add_field(name="Total Denied",         value=str(len(all_denied)),                            inline=True)
    embed.add_field(name="Pending",              value=str(len(all_pending)),                           inline=True)
    embed.add_field(name="Lifetime Worth",       value=f"{CURRENCY_SYMBOL}{calc_worth(len(all_approved)):,.2f}", inline=True)
    embed.add_field(name="Lifetime Commission",  value=f"{CURRENCY_SYMBOL}{total_comm:,.2f}",           inline=True)
    embed.add_field(name="Total Points",         value=str(total_pts),                                  inline=True)
    embed.add_field(name="SotW Wins",            value=str(sotw_wins),                                  inline=True)

    if badges:
        embed.add_field(
            name="Achievements",
            value="  ".join(f"{ACHIEVEMENT_EMOJI.get(b, '🏅')} {b}" for b in badges),
            inline=False
        )
    await i.response.send_message(embed=embed)


@tree.command(name="exchange_leaderboard", description="Top staff by exchanges for a time period.")
@app_commands.describe(period="day / week / month / alltime")
async def exchange_leaderboard(i: discord.Interaction, period: str = "week"):
    period = period.lower()
    if period not in ("day", "week", "month", "alltime"):
        return await i.response.send_message("Period must be: day / week / month / alltime", ephemeral=True)

    data  = load_data()
    tally = defaultdict(int)
    for ex in data["exchanges"]:
        if ex["status"] != "approved":
            continue
        if filter_by_period([ex], period):
            tally[ex["requester_id"]] += 1
            tally[ex["target_id"]]   += 1

    if not tally:
        return await i.response.send_message("No data for this period.", ephemeral=True)

    label        = {"day": "Today", "week": "This Week", "month": "This Month", "alltime": "All Time"}[period]
    sorted_tally = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    embed        = discord.Embed(title=f"Exchange Leaderboard — {label}", color=0xF1C40F)
    medals       = ["🥇", "🥈", "🥉"]
    for rank, (uid, count) in enumerate(sorted_tally[:10]):
        medal = medals[rank] if rank < 3 else f"#{rank+1}"
        embed.add_field(
            name=f"{medal}  <@{uid}>",
            value=f"**{count}** exchanges | Worth {CURRENCY_SYMBOL}{calc_worth(count):,.0f} | Commission {CURRENCY_SYMBOL}{calc_commission(calc_worth(count)):,.0f}",
            inline=False
        )
    await i.response.send_message(embed=embed)


@tree.command(name="exchange_summary", description="[Admin] Overall exchange summary for a time period.")
@app_commands.describe(period="day / week / month / alltime")
async def exchange_summary(i: discord.Interaction, period: str = "week"):
    if not is_admin(i):
        return await i.response.send_message("Admins only.", ephemeral=True)
    period = period.lower()
    data   = load_data()
    exs    = filter_by_period([e for e in data["exchanges"] if e["status"] == "approved"], period)
    label  = {"day": "Today", "week": "This Week", "month": "This Month", "alltime": "All Time"}[period]

    embed  = discord.Embed(title=f"Exchange Summary — {label}", color=0x2ECC71)
    embed.add_field(name="Approved", value=str(len(exs)), inline=True)
    embed.add_field(name="Pending",  value=str(len([e for e in data["exchanges"] if e["status"] == "pending"])), inline=True)
    embed.add_field(name="Denied",   value=str(len([e for e in data["exchanges"] if e["status"] == "denied"])),  inline=True)
    embed.add_field(name="Total Worth",      value=f"{CURRENCY_SYMBOL}{calc_worth(len(exs)):,.2f}",            inline=True)
    embed.add_field(name="Total Commission", value=f"{CURRENCY_SYMBOL}{calc_commission(calc_worth(len(exs))):,.2f}", inline=True)
    embed.add_field(name="Exchange Value",   value=f"{CURRENCY_SYMBOL}{EXCHANGE_WORTH}/each @ {int(COMMISSION_RATE*100)}% commission", inline=True)
    await i.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════
#  STAFF OF THE WEEK
# ══════════════════════════════════════════════════════════════

@tree.command(name="sotw_nominate", description="Nominate a staff member for Staff of the Week.")
@app_commands.describe(member="Who to nominate", reason="Why they deserve it")
async def sotw_nominate(i: discord.Interaction, member: discord.Member, reason: str):
    if not is_staff(i):
        return await i.response.send_message("Staff only.", ephemeral=True)
    if member.id == i.user.id:
        return await i.response.send_message("Cannot nominate yourself.", ephemeral=True)
    data  = load_data()
    wk    = week_key()
    noms  = data["sotw_nominations"].setdefault(wk, {})
    if str(i.user.id) in noms:
        return await i.response.send_message("Already nominated someone this week.", ephemeral=True)
    noms[str(i.user.id)] = {"nominee_id": member.id, "reason": reason}
    save_data(data)
    embed = discord.Embed(title="SotW Nomination", description=f"{i.user.mention} nominated **{member.display_name}**!", color=0xF1C40F)
    embed.add_field(name="Reason", value=reason)
    embed.set_thumbnail(url=member.display_avatar.url)
    await i.response.send_message(embed=embed)


@tree.command(name="sotw_vote", description="Vote for Staff of the Week.")
@app_commands.describe(member="Who you're voting for")
async def sotw_vote(i: discord.Interaction, member: discord.Member):
    data  = load_data()
    wk    = week_key()
    votes = data["sotw_votes"].setdefault(wk, {})
    if str(i.user.id) in votes:
        return await i.response.send_message("Already voted this week.", ephemeral=True)
    votes[str(i.user.id)] = member.id
    save_data(data)
    await i.response.send_message(f"Voted for **{member.display_name}**!", ephemeral=True)


@tree.command(name="sotw_results", description="Current SotW standings.")
async def sotw_results(i: discord.Interaction):
    data  = load_data()
    wk    = week_key()
    votes = data["sotw_votes"].get(wk, {})
    if not votes:
        return await i.response.send_message("No votes yet this week.", ephemeral=True)
    tally = defaultdict(int)
    for _, v in votes.items():
        tally[v] += 1
    sorted_t = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    embed    = discord.Embed(title="SotW Standings", color=0xF1C40F)
    embed.set_footer(text=f"Week {wk} | {len(votes)} votes")
    medals = ["🥇", "🥈", "🥉"]
    for rank, (uid, cnt) in enumerate(sorted_t[:5]):
        medal = medals[rank] if rank < 3 else f"#{rank+1}"
        embed.add_field(name=f"{medal} <@{uid}>", value=f"{cnt} vote(s)", inline=False)
    await i.response.send_message(embed=embed)


@tree.command(name="sotw_announce", description="[Admin] Announce SotW winner, party, and commission bonus.")
@app_commands.describe(commission_bonus="One-time bonus commission for the winner (e.g. 200)")
async def sotw_announce(i: discord.Interaction, commission_bonus: float = 0.0):
    if not is_admin(i):
        return await i.response.send_message("Admins only.", ephemeral=True)
    data  = load_data()
    wk    = week_key()
    votes = data["sotw_votes"].get(wk, {})
    if not votes:
        return await i.response.send_message("No votes this week.", ephemeral=True)

    tally   = defaultdict(int)
    for _, v in votes.items():
        tally[v] += 1
    sorted_t = sorted(tally.items(), key=lambda x: x[1], reverse=True)

    winner_id    = sorted_t[0][0]
    winner_votes = sorted_t[0][1]
    runner_id    = sorted_t[1][0] if len(sorted_t) > 1 else None
    winner       = i.guild.get_member(winner_id)

    # Award points & commission
    wk_str = str(winner_id)
    data["points"][wk_str]      = data["points"].get(wk_str, 0) + POINTS_SOTW_WIN
    data["commissions"][wk_str] = data["commissions"].get(wk_str, 0.0) + commission_bonus

    if runner_id:
        ru = str(runner_id)
        data["points"][ru] = data["points"].get(ru, 0) + POINTS_SOTW_RUNNER_UP

    data["sotw_history"].append({
        "week":         wk,
        "winner_id":    winner_id,
        "votes":        winner_votes,
        "runner_up_id": runner_id,
        "bonus":        commission_bonus,
    })
    badges = check_achievements(data, winner_id)
    save_data(data)
    await notify_badges(i.guild, winner_id, badges)

    # Announcement embed
    embed = discord.Embed(
        title="STAFF OF THE WEEK",
        description=f"Congratulations to {winner.mention if winner else f'<@{winner_id}>'}! You are this week's Staff of the Week!",
        color=0xFFD700
    )
    embed.add_field(name="Votes",           value=str(winner_votes),     inline=True)
    embed.add_field(name="Points Awarded",  value=f"+{POINTS_SOTW_WIN}", inline=True)
    if commission_bonus > 0:
        embed.add_field(name="Bonus Commission", value=f"+{CURRENCY_SYMBOL}{commission_bonus:.2f}", inline=True)
    if runner_id:
        runner = i.guild.get_member(runner_id)
        embed.add_field(
            name="Runner-Up",
            value=f"{runner.mention if runner else f'<@{runner_id}>'} (+{POINTS_SOTW_RUNNER_UP} pts)",
            inline=False
        )
    if winner:
        embed.set_thumbnail(url=winner.display_avatar.url)
    embed.set_footer(text=f"Week {wk}")
    await i.response.send_message(embed=embed)

    # Party announcement
    ch = i.channel
    if SOTW_CHANNEL_ID:
        sc = i.guild.get_channel(SOTW_CHANNEL_ID)
        if sc:
            ch = sc

    party_embed = discord.Embed(
        title="PARTY TIME!",
        description=(
            f"Everyone give a round of applause for our Staff of the Week — "
            f"{winner.mention if winner else f'<@{winner_id}>'}!\n\n"
            f"The SotW Party is now LIVE! Come celebrate and appreciate your teammate. They earned it!"
        ),
        color=0xFF6FD8
    )
    party_embed.set_footer(text="React with the emojis below to celebrate!")
    msg = await ch.send(embed=party_embed)
    await msg.add_reaction("🎉")
    await msg.add_reaction("🥳")
    await msg.add_reaction("⭐")
    await msg.add_reaction("🏆")

    await audit_log(f"SotW: {winner} wins week {wk} | {winner_votes} votes | Bonus: {CURRENCY_SYMBOL}{commission_bonus}")

    # DM winner
    if winner:
        try:
            await winner.send(
                f"You've been named Staff of the Week for {wk}!\n"
                f"You earned +{POINTS_SOTW_WIN} points"
                + (f" and a {CURRENCY_SYMBOL}{commission_bonus:.2f} commission bonus!" if commission_bonus > 0 else "!")
                + "\nThe party is starting now — enjoy!"
            )
        except Exception:
            pass


@tree.command(name="sotw_history", description="View past SotW winners.")
async def sotw_history_cmd(i: discord.Interaction):
    data = load_data()
    hist = data["sotw_history"][-10:]
    if not hist:
        return await i.response.send_message("No history yet.", ephemeral=True)
    embed = discord.Embed(title="SotW History", color=0xFFD700)
    for entry in reversed(hist):
        val = f"<@{entry['winner_id']}> — {entry['votes']} vote(s)"
        if entry.get("runner_up_id"):
            val += f"\nRunner-up: <@{entry['runner_up_id']}>"
        if entry.get("bonus", 0) > 0:
            val += f"\nBonus: {CURRENCY_SYMBOL}{entry['bonus']:.2f}"
        embed.add_field(name=f"Week {entry['week']}", value=val, inline=False)
    await i.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════
#  POINTS & COMMISSION
# ══════════════════════════════════════════════════════════════

@tree.command(name="mypoints", description="View your full staff profile — points, commission, stats.")
async def mypoints(i: discord.Interaction):
    data       = load_data()
    uid        = str(i.user.id)
    pts        = data["points"].get(uid, 0)
    comm       = data["commissions"].get(uid, 0.0)
    badges     = data["achievements"].get(uid, [])
    all_ex     = exchanges_for_user(data, i.user.id, "approved")
    week_ex    = filter_by_period(all_ex, "week")
    month_ex   = filter_by_period(all_ex, "month")
    sotw_wins  = sum(1 for h in data["sotw_history"] if h["winner_id"] == i.user.id)

    embed = discord.Embed(title=f"My Staff Profile — {i.user.display_name}", color=0x5865F2)
    embed.set_thumbnail(url=i.user.display_avatar.url)
    embed.add_field(name="Points",             value=str(pts),                                          inline=True)
    embed.add_field(name="Total Commission",   value=f"{CURRENCY_SYMBOL}{comm:,.2f}",                   inline=True)
    embed.add_field(name="SotW Wins",          value=str(sotw_wins),                                    inline=True)
    embed.add_field(name="Exchanges Today",    value=str(len(filter_by_period(all_ex, "day"))),          inline=True)
    embed.add_field(name="Exchanges This Week",value=str(len(week_ex)),                                  inline=True)
    embed.add_field(name="Exchanges This Month",value=str(len(month_ex)),                               inline=True)
    embed.add_field(name="Week Worth",         value=f"{CURRENCY_SYMBOL}{calc_worth(len(week_ex)):,.2f}", inline=True)
    embed.add_field(name="Month Worth",        value=f"{CURRENCY_SYMBOL}{calc_worth(len(month_ex)):,.2f}", inline=True)
    embed.add_field(name="Lifetime Worth",     value=f"{CURRENCY_SYMBOL}{calc_worth(len(all_ex)):,.2f}",  inline=True)
    if badges:
        embed.add_field(
            name="Achievements",
            value="  ".join(f"{ACHIEVEMENT_EMOJI.get(b,'🏅')} {b}" for b in badges),
            inline=False
        )
    await i.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="points_add", description="[Admin] Manually add points.")
@app_commands.describe(member="Target", amount="Points to add", reason="Why")
async def points_add(i: discord.Interaction, member: discord.Member, amount: int, reason: str = "Manual award"):
    if not is_admin(i):
        return await i.response.send_message("Admins only.", ephemeral=True)
    data = load_data()
    k    = str(member.id)
    data["points"][k] = data["points"].get(k, 0) + amount
    badges = check_achievements(data, member.id)
    save_data(data)
    await notify_badges(i.guild, member.id, badges)
    await i.response.send_message(
        f"Added {amount} pts to {member.mention}. Total: {data['points'][k]} | {reason}"
    )
    await audit_log(f"Points +{amount} to {member} by {i.user} | {reason}")


@tree.command(name="commission_add", description="[Admin] Manually add commission.")
@app_commands.describe(member="Target", amount="Commission amount", reason="Why")
async def commission_add(i: discord.Interaction, member: discord.Member, amount: float, reason: str = "Manual award"):
    if not is_admin(i):
        return await i.response.send_message("Admins only.", ephemeral=True)
    data = load_data()
    k    = str(member.id)
    data["commissions"][k] = data["commissions"].get(k, 0.0) + amount
    save_data(data)
    await i.response.send_message(
        f"Added {CURRENCY_SYMBOL}{amount:.2f} commission to {member.mention}. Total: {CURRENCY_SYMBOL}{data['commissions'][k]:,.2f} | {reason}"
    )
    await audit_log(f"Commission +{CURRENCY_SYMBOL}{amount:.2f} to {member} by {i.user} | {reason}")


@tree.command(name="leaderboard", description="Staff points leaderboard.")
async def leaderboard(i: discord.Interaction):
    data = load_data()
    pts  = data["points"]
    if not pts:
        return await i.response.send_message("No points yet!", ephemeral=True)
    sorted_pts = sorted(pts.items(), key=lambda x: x[1], reverse=True)
    embed      = discord.Embed(title="Points Leaderboard", color=0x2ECC71)
    medals     = ["🥇", "🥈", "🥉"]
    for rank, (uid, p) in enumerate(sorted_pts[:10]):
        medal = medals[rank] if rank < 3 else f"#{rank+1}"
        embed.add_field(name=f"{medal} <@{uid}>", value=f"{p} pts", inline=False)
    await i.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════
#  WARNINGS
# ══════════════════════════════════════════════════════════════

@tree.command(name="warn", description="[Admin] Issue a warning.")
@app_commands.describe(member="Who to warn", reason="Reason")
async def warn(i: discord.Interaction, member: discord.Member, reason: str):
    if not is_admin(i):
        return await i.response.send_message("Admins only.", ephemeral=True)
    data = load_data()
    k    = str(member.id)
    data["warnings"].setdefault(k, []).append({"reason": reason, "admin_id": i.user.id, "date": fmt_dt(now_utc())})
    count = len(data["warnings"][k])
    save_data(data)
    embed = discord.Embed(title="Warning Issued", color=0xE74C3C)
    embed.add_field(name="Member",     value=member.mention, inline=True)
    embed.add_field(name="Warning #",  value=str(count),     inline=True)
    embed.add_field(name="Reason",     value=reason,         inline=False)
    embed.add_field(name="Issued by",  value=i.user.mention, inline=True)
    if count >= 3:
        embed.add_field(name="ALERT", value="3+ warnings! Consider further action.", inline=False)
    await i.response.send_message(embed=embed)
    await audit_log(f"Warning #{count} issued to {member} by {i.user} | {reason}")
    try:
        await member.send(f"You received warning #{count} from {i.user.display_name}.\nReason: {reason}")
    except Exception:
        pass


@tree.command(name="warnings", description="View warnings for a member.")
@app_commands.describe(member="Leave blank for yourself")
async def warnings(i: discord.Interaction, member: discord.Member = None):
    member = member or i.user
    if member.id != i.user.id and not is_admin(i):
        return await i.response.send_message("Admins only for others.", ephemeral=True)
    data   = load_data()
    warns  = data["warnings"].get(str(member.id), [])
    embed  = discord.Embed(title=f"Warnings — {member.display_name}", color=0xE74C3C)
    if not warns:
        embed.description = "No warnings — clean record!"
    else:
        for idx, w in enumerate(warns, 1):
            embed.add_field(name=f"#{idx} — {w['date']}", value=f"Reason: {w['reason']}\nBy: <@{w['admin_id']}>", inline=False)
    await i.response.send_message(embed=embed, ephemeral=(member.id == i.user.id))


@tree.command(name="warning_clear", description="[Admin] Clear all warnings for a member.")
async def warning_clear(i: discord.Interaction, member: discord.Member):
    if not is_admin(i):
        return await i.response.send_message("Admins only.", ephemeral=True)
    data = load_data()
    data["warnings"][str(member.id)] = []
    save_data(data)
    await i.response.send_message(f"All warnings cleared for {member.mention}.")
    await audit_log(f"Warnings cleared for {member} by {i.user}")

# ══════════════════════════════════════════════════════════════
#  ATTENDANCE
# ══════════════════════════════════════════════════════════════

@tree.command(name="checkin", description="Clock in for your shift.")
async def checkin(i: discord.Interaction):
    if not is_staff(i):
        return await i.response.send_message("Staff only.", ephemeral=True)
    data = load_data()
    k    = str(i.user.id)
    dk   = day_key()
    att  = data["attendance"].setdefault(k, [])
    for r in reversed(att):
        if r["date"] == dk and not r.get("check_out"):
            return await i.response.send_message("Already checked in! Use /checkout first.", ephemeral=True)
    att.append({"date": dk, "check_in": fmt_dt(now_utc()), "check_out": None})
    save_data(data)
    await i.response.send_message(f"Checked in at `{fmt_dt(now_utc())}`. Have a great shift!", ephemeral=True)


@tree.command(name="checkout", description="Clock out from your shift.")
async def checkout(i: discord.Interaction):
    if not is_staff(i):
        return await i.response.send_message("Staff only.", ephemeral=True)
    data = load_data()
    k    = str(i.user.id)
    dk   = day_key()
    att  = data["attendance"].get(k, [])
    for r in reversed(att):
        if r["date"] == dk and not r.get("check_out"):
            r["check_out"] = fmt_dt(now_utc())
            ci  = datetime.strptime(r["check_in"],  "%Y-%m-%d %H:%M UTC")
            co  = datetime.strptime(r["check_out"], "%Y-%m-%d %H:%M UTC")
            dur = co - ci
            h, rem = divmod(int(dur.total_seconds()), 3600)
            m       = rem // 60
            save_data(data)
            return await i.response.send_message(
                f"Checked out at `{r['check_out']}`.\nShift duration: **{h}h {m}m**", ephemeral=True
            )
    await i.response.send_message("No active check-in found for today.", ephemeral=True)


@tree.command(name="attendance", description="View attendance records.")
@app_commands.describe(member="Leave blank for yourself")
async def attendance_view(i: discord.Interaction, member: discord.Member = None):
    member  = member or i.user
    if member.id != i.user.id and not is_admin(i):
        return await i.response.send_message("Admins only for others.", ephemeral=True)
    data    = load_data()
    records = data["attendance"].get(str(member.id), [])[-7:]
    embed   = discord.Embed(title=f"Attendance — {member.display_name}", color=0x3498DB)
    if not records:
        embed.description = "No records found."
    else:
        for r in reversed(records):
            status = "Completed" if r.get("check_out") else "Currently In"
            embed.add_field(
                name=r["date"],
                value=f"In: {r['check_in']}\nOut: {r.get('check_out', '—')}\n{status}",
                inline=False
            )
    await i.response.send_message(embed=embed, ephemeral=(member.id == i.user.id))

# ══════════════════════════════════════════════════════════════
#  ACHIEVEMENTS
# ══════════════════════════════════════════════════════════════

@tree.command(name="achievements", description="View achievements and badges.")
@app_commands.describe(member="Leave blank for yourself")
async def achievements_view(i: discord.Interaction, member: discord.Member = None):
    member = member or i.user
    data   = load_data()
    earned = set(data["achievements"].get(str(member.id), []))
    embed  = discord.Embed(title=f"Achievements — {member.display_name}", color=0x9B59B6)
    embed.set_thumbnail(url=member.display_avatar.url)
    if not earned:
        embed.description = "No achievements yet — keep going!"
    for name in ACHIEVEMENTS:
        emoji = ACHIEVEMENT_EMOJI.get(name, "🏅")
        if name in earned:
            embed.add_field(name=f"{emoji} {name}", value="Unlocked", inline=True)
        else:
            embed.add_field(name=f"(locked) {name}", value="Not yet", inline=True)
    await i.response.send_message(embed=embed)

# ══════════════════════════════════════════════════════════════
#  STAFF OF THE MONTH
# ══════════════════════════════════════════════════════════════

@tree.command(name="sotm_announce", description="[Admin] Announce Staff of the Month based on weekly wins.")
async def sotm_announce(i: discord.Interaction):
    if not is_admin(i):
        return await i.response.send_message("Admins only.", ephemeral=True)
    data = load_data()
    mk   = month_key()
    wins = defaultdict(int)
    # Count this month's weekly wins (last ~4 weeks)
    now_wk = int(week_key().split("W")[1])
    for entry in data["sotw_history"]:
        try:
            entry_wk = int(entry["week"].split("W")[1])
            if 0 <= now_wk - entry_wk <= 4:
                wins[entry["winner_id"]] += 1
        except Exception:
            pass
    if not wins:
        return await i.response.send_message("No SotW wins this month to base on.", ephemeral=True)

    winner_id   = max(wins, key=wins.get)
    weekly_wins = wins[winner_id]
    winner      = i.guild.get_member(winner_id)

    data["points"][str(winner_id)] = data["points"].get(str(winner_id), 0) + 50
    data["sotw_month_history"].append({"month": mk, "winner_id": winner_id, "weekly_wins": weekly_wins})
    badges = check_achievements(data, winner_id)
    save_data(data)
    await notify_badges(i.guild, winner_id, badges)

    embed = discord.Embed(
        title="STAFF OF THE MONTH",
        description=f"This month's Staff of the Month is {winner.mention if winner else f'<@{winner_id}>'}!",
        color=0xFF6B00
    )
    embed.add_field(name="Weekly Wins", value=str(weekly_wins), inline=True)
    embed.add_field(name="Bonus Points", value="+50 pts",       inline=True)
    if winner:
        embed.set_thumbnail(url=winner.display_avatar.url)
    embed.set_footer(text=f"Month: {mk}")
    await i.response.send_message(embed=embed)
    await audit_log(f"SotM: {winner} | Month {mk} | {weekly_wins} weekly wins")

# ══════════════════════════════════════════════════════════════
#  SCHEDULED TASKS
# ══════════════════════════════════════════════════════════════

def get_staff_channel(guild: discord.Guild):
    if SOTW_CHANNEL_ID:
        ch = guild.get_channel(SOTW_CHANNEL_ID)
        if ch:
            return ch
    return next(
        (c for c in guild.text_channels if "staff" in c.name.lower() or "general" in c.name.lower()),
        None
    )

@tasks.loop(hours=168)
async def weekly_sotw_reminder():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = get_staff_channel(guild)
    if ch:
        embed = discord.Embed(
            title="Staff of the Week — Voting Open!",
            description=(
                "Use /sotw_nominate to nominate a teammate!\n"
                "Use /sotw_vote to cast your vote!\n"
                "Use /sotw_results to see the standings.\n\n"
                "Winner gets a Party + Commission Bonus!"
            ),
            color=0xF1C40F
        )
        await ch.send(embed=embed)


@tasks.loop(hours=168)
async def weekly_exchange_summary():
    """Automatically posts a weekly exchange leaderboard."""
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    data  = load_data()
    exs   = filter_by_period([e for e in data["exchanges"] if e["status"] == "approved"], "week")
    tally = defaultdict(int)
    for ex in exs:
        tally[ex["requester_id"]] += 1
        tally[ex["target_id"]]   += 1
    if not tally:
        return
    sorted_t = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    embed    = discord.Embed(title="Weekly Exchange Summary", color=0x2ECC71)
    embed.set_footer(text=f"Week {week_key()} | {CURRENCY_SYMBOL}{EXCHANGE_WORTH}/exchange @ {int(COMMISSION_RATE*100)}% commission")
    medals = ["🥇", "🥈", "🥉"]
    for rank, (uid, cnt) in enumerate(sorted_t[:5]):
        medal = medals[rank] if rank < 3 else f"#{rank+1}"
        embed.add_field(
            name=f"{medal} <@{uid}>",
            value=f"{cnt} exchanges | Worth {CURRENCY_SYMBOL}{calc_worth(cnt):,.0f} | Commission {CURRENCY_SYMBOL}{calc_commission(calc_worth(cnt)):,.0f}",
            inline=False
        )
    ch = get_staff_channel(guild)
    if ch:
        await ch.send(embed=embed)


@tasks.loop(hours=720)
async def monthly_sotm_reminder():
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = get_staff_channel(guild)
    if ch:
        await ch.send("Month End: Admins — use /sotm_announce to crown the Staff of the Month!")

# ──────────────────────────────────────────────────────────────
#  RUN
# ──────────────────────────────────────────────────────────────
bot.run(TOKEN)
