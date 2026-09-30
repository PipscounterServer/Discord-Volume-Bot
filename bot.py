"""
Discord IB Volume Role-Demotion Bot (v2 - rolling per-member cycles)
------------------------------------------------------------
Each member's "month" runs from THEIR OWN join date, not a shared calendar
month. Every time this bot runs, it checks each trading member individually:

  - How many full months have passed since their cycle start?
  - If 0: not due yet, skip (this naturally protects new members during
    their first partial month).
  - If 1+: they need MIN_LOTS lots traded since their cycle start. If they
    meet it, their cycle quietly rolls forward with a fresh baseline. If
    not, they are immediately moved from the ELITE_ROLE_NAME role to the
    DEMOTE_TO_ROLE_NAME role (e.g. "Elite Student" -> "Freshman") - no
    grace period, and no kick from the server at all.

This works even if you update the CSV data irregularly - the bot always
calculates the correct number of months elapsed for each member.

Requirements:
    pip install discord.py gspread oauth2client python-dotenv python-dateutil
"""

import os
import logging
from datetime import datetime, date, timezone

import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
GOOGLE_CREDS_FILE = os.getenv("GOOGLE_CREDS_FILE", "credentials.json")
SHEET_NAME = os.getenv("SHEET_NAME", "IB Volume Tracker")

SHEET_MEMBER_MAPPING = "Member Mapping"
SHEET_RAW_PASTE = "Monthly Raw Paste"
SHEET_STATUS_OVERVIEW = "Status Overview"
SHEET_KICK_LOG = "Kick Log"
SHEET_SETTINGS = "Settings"

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID")
LOG_CHANNEL_ID = int(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None

NEW_MEMBER_ROLE_NAME = os.getenv("NEW_MEMBER_ROLE_NAME", "Freshman")
ELITE_ROLE_NAME = os.getenv("ELITE_ROLE_NAME", "Elite Student")
DEMOTE_TO_ROLE_NAME = os.getenv("DEMOTE_TO_ROLE_NAME", "Freshman")

# One-time safety switch for your first test only - not a per-run confirmation.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("volume-bot")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def get_client():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_FILE, scope)
    return gspread.authorize(creds)


def parse_date(value):
    """Parse a date from sheet cell content, handling a few common formats."""
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def months_elapsed(start_date, today):
    """Number of full calendar months between start_date and today."""
    if today < start_date:
        return 0
    delta = relativedelta(today, start_date)
    return delta.years * 12 + delta.months


async def run_kick_check(triggered_by: str = "scheduled"):
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        logger.error("Guild not found. Check GUILD_ID.")
        return {"error": "Guild not found"}

    try:
        client = get_client()
        sh = client.open(SHEET_NAME)
        mapping_ws = sh.worksheet(SHEET_MEMBER_MAPPING)
        raw_ws = sh.worksheet(SHEET_RAW_PASTE)
        status_ws = sh.worksheet(SHEET_STATUS_OVERVIEW)
        kicklog_ws = sh.worksheet(SHEET_KICK_LOG)
        settings_ws = sh.worksheet(SHEET_SETTINGS)

        mapping_records = mapping_ws.get_all_records()
        raw_records = raw_ws.get_all_records()
        settings_records = settings_ws.get_all_records()
        previous_status_records = status_ws.get_all_records()
    except Exception as e:
        logger.exception("Failed to read sheet")
        return {"error": f"Sheet read failed: {e}"}

    # Build lookup of each member's last known cycle baseline from the previous run.
    # This is what makes cycles actually roll forward instead of resetting to join
    # date every single time.
    previous_cycle_by_discord_id = {}
    for row in previous_status_records:
        did = str(row.get("Discord ID", "")).strip()
        cycle_start_str = str(row.get("Cycle Start Date", "")).strip()
        cycle_vol_str = str(row.get("Cycle Start Volume", "")).strip()
        prev_status = str(row.get("Status", "")).strip()
        if did and cycle_start_str and prev_status in ("OK", "Not due yet", "ANOMALY - volume decreased, check CSV data"):
            parsed = parse_date(cycle_start_str)
            if parsed:
                try:
                    cycle_vol = float(cycle_vol_str) if cycle_vol_str else 0.0
                except ValueError:
                    cycle_vol = 0.0
                previous_cycle_by_discord_id[did] = (parsed, cycle_vol)

    min_lots = 1.0
    for row in settings_records:
        if str(row.get("Setting", "")).strip() == "Minimum Lots Per Month":
            try:
                min_lots = float(row.get("Value", 1))
            except (ValueError, TypeError):
                pass

    volume_by_user_id = {}
    for row in raw_records:
        uid = str(row.get("user_id", "")).strip().lower()
        if uid:
            try:
                volume_by_user_id[uid] = float(row.get("volume", 0) or 0)
            except ValueError:
                pass

    today = datetime.now(timezone.utc).date()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    results = {"kicked": [], "ok": [], "not_due": [], "skipped_not_found": [], "errors": []}
    status_rows = []  # rows to write to Status Overview (overwrites each run)

    for row in mapping_records:
        discord_id_raw = str(row.get("Discord ID", "")).strip()
        broker_id = str(row.get("Broker user_id", "")).strip()
        username = str(row.get("Discord Username", "")).strip()
        exempt = str(row.get("Exempt (Y/N)", "")).strip().upper() == "Y"
        join_date = parse_date(row.get("Join Date", ""))

        if not discord_id_raw:
            continue

        # No broker account = owner's friend / non-trader -> always exempt, never evaluated
        if not broker_id:
            status_rows.append([discord_id_raw, username, "", "", "", "", "", "", "Exempt (no broker account)", now_str])
            continue

        if exempt:
            status_rows.append([discord_id_raw, username, "", "", "", "", "", "", "Exempt", now_str])
            continue

        if join_date is None:
            results["errors"].append(f"{username or discord_id_raw}: invalid or missing Join Date")
            status_rows.append([discord_id_raw, username, "", "", "", "", "", "", "Error - invalid join date", now_str])
            continue

        current_volume = volume_by_user_id.get(broker_id.lower())
        if current_volume is None:
            # No CSV data for this user_id yet - can't evaluate, not an error, just not due
            status_rows.append([discord_id_raw, username, str(join_date), "", "", "", "", "", "No trade data yet", now_str])
            continue

        # Cycle baseline: use the last confirmed baseline from the previous run if we
        # have one, otherwise this is their very first check - start from join date
        # with a baseline volume of 0.
        if discord_id_raw in previous_cycle_by_discord_id:
            cycle_start_date, cycle_start_volume = previous_cycle_by_discord_id[discord_id_raw]
        else:
            cycle_start_date, cycle_start_volume = join_date, 0.0

        # Anomaly check: cumulative volume should never go DOWN. If it did, the
        # CSV pasted this run is likely stale, filtered, or otherwise wrong - skip
        # evaluating this person entirely rather than risk a wrongful kick on bad data.
        if current_volume < cycle_start_volume:
            results["errors"].append(
                f"{username or discord_id_raw}: volume dropped from {cycle_start_volume} to "
                f"{current_volume} since last check - looks like bad/stale CSV data, skipped"
            )
            status_rows.append([
                discord_id_raw, username, str(cycle_start_date), cycle_start_volume,
                current_volume, current_volume - cycle_start_volume, "", "",
                "ANOMALY - volume decreased, check CSV data", now_str
            ])
            continue

        elapsed = months_elapsed(cycle_start_date, today)

        if elapsed < 1:
            results["not_due"].append(username or discord_id_raw)
            status_rows.append([
                discord_id_raw, username, str(cycle_start_date), cycle_start_volume,
                current_volume, current_volume - cycle_start_volume, elapsed, 0,
                "Not due yet", now_str
            ])
            continue

        required = min_lots
        volume_this_cycle = current_volume - cycle_start_volume

        if volume_this_cycle >= required:
            results["ok"].append(username or discord_id_raw)
            # Roll the cycle forward: new baseline is (old start + elapsed months) and
            # (current volume) - so future checks measure fresh volume, no carryover
            # of any surplus lots into the next period.
            new_cycle_start_date = cycle_start_date + relativedelta(months=elapsed)
            status_rows.append([
                discord_id_raw, username, str(new_cycle_start_date), current_volume,
                current_volume, volume_this_cycle, elapsed, required,
                "OK", now_str
            ])
            continue

        # Below required - demote role instead of kicking
        member = guild.get_member(int(discord_id_raw)) if discord_id_raw.isdigit() else None
        if member is None and discord_id_raw.isdigit():
            try:
                member = await guild.fetch_member(int(discord_id_raw))
            except discord.NotFound:
                results["skipped_not_found"].append(discord_id_raw)
                status_rows.append([
                    discord_id_raw, username, str(cycle_start_date), cycle_start_volume,
                    current_volume, volume_this_cycle, elapsed, required,
                    "Not in server", now_str
                ])
                continue
            except Exception as e:
                results["errors"].append(f"Row for {username}: fetch_member failed: {e}")
                continue

        if DRY_RUN:
            logger.info(
                f"[DRY RUN] Would demote {member or discord_id_raw} from '{ELITE_ROLE_NAME}' "
                f"to '{DEMOTE_TO_ROLE_NAME}' "
                f"(needed {required} lots over {elapsed} month(s), had {volume_this_cycle})"
            )
            status_rows.append([
                discord_id_raw, username, str(cycle_start_date), cycle_start_volume,
                current_volume, volume_this_cycle, elapsed, required,
                "Would demote (dry run)", now_str
            ])
            continue

        try:
            elite_role = discord.utils.get(guild.roles, name=ELITE_ROLE_NAME)
            demote_role = discord.utils.get(guild.roles, name=DEMOTE_TO_ROLE_NAME)
            if demote_role is None:
                results["errors"].append(
                    f"Could not find role '{DEMOTE_TO_ROLE_NAME}' to demote {member} into"
                )
                status_rows.append([
                    discord_id_raw, username, str(cycle_start_date), cycle_start_volume,
                    current_volume, volume_this_cycle, elapsed, required,
                    "Error - demote role not found", now_str
                ])
                continue

            if elite_role is not None and elite_role in member.roles:
                await member.remove_roles(
                    elite_role,
                    reason=f"Below required volume: {volume_this_cycle} / {required} lots over {elapsed} month(s)"
                )
            if demote_role not in member.roles:
                await member.add_roles(
                    demote_role,
                    reason=f"Below required volume: {volume_this_cycle} / {required} lots over {elapsed} month(s)"
                )

            results["kicked"].append(f"{member} (needed {required}, had {volume_this_cycle})")
            kicklog_ws.append_row([
                now_str, discord_id_raw, username, elapsed, required, volume_this_cycle,
                f"Below required volume for elapsed period - moved '{ELITE_ROLE_NAME}' -> '{DEMOTE_TO_ROLE_NAME}'"
            ])
            status_rows.append([
                discord_id_raw, username, str(cycle_start_date), cycle_start_volume,
                current_volume, volume_this_cycle, elapsed, required,
                "Demoted", now_str
            ])
        except discord.Forbidden:
            results["errors"].append(f"Missing permission to change roles for {member}")
        except Exception as e:
            results["errors"].append(f"Role change failed for {member}: {e}")

    # Rewrite Status Overview fully each run (simple, avoids stale rows), including
    # during dry runs so you can preview what would happen. Written as ONE batched
    # write instead of one API call per row, to avoid hitting Google's per-minute
    # write quota on servers with many members.
    try:
        header = [
            "Discord ID", "Discord Username", "Cycle Start Date", "Cycle Start Volume",
            "Current Volume", "Volume This Cycle", "Months Elapsed", "Lots Required",
            "Status", "Last Checked"
        ]
        status_ws.clear()
        status_ws.update([header] + status_rows, value_input_option="USER_ENTERED")
    except Exception as e:
        results["errors"].append(f"Failed to update Status Overview: {e}")

    await post_summary(results, triggered_by)

    if results["errors"]:
        for err in results["errors"]:
            logger.error(f"Detail: {err}")

    if not results["kicked"] and not results["errors"]:
        logger.info("Check complete: no members needed demoting.")
    else:
        logger.info(
            f"Check complete: demoted={len(results['kicked'])}, "
            f"ok={len(results['ok'])}, not_due={len(results['not_due'])}, "
            f"errors={len(results['errors'])}"
        )

    return results


async def post_summary(results, triggered_by):
    if not LOG_CHANNEL_ID:
        return
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        return

    mode = "DRY RUN" if DRY_RUN else "LIVE"
    lines = [f"**Volume check complete** ({mode}, triggered by: {triggered_by})"]
    lines.append(f"Demoted: {len(results.get('kicked', []))}")
    lines.append(f"OK: {len(results.get('ok', []))}")
    lines.append(f"Not due yet: {len(results.get('not_due', []))}")
    lines.append(f"Not found in server: {len(results.get('skipped_not_found', []))}")
    if results.get("errors"):
        lines.append(f"Errors: {len(results['errors'])}")
        for err in results["errors"][:10]:
            lines.append(f"  - {err}")
    if results.get("kicked"):
        lines.append("\n**Demoted users:**")
        for k in results["kicked"][:20]:
            lines.append(f"  - {k}")

    await channel.send("\n".join(lines))


@bot.command(name="swaprole")
@commands.has_permissions(manage_roles=True)
async def swaprole(ctx, *, role_names: str):
    """Usage: !swaprole Old Role Name -> New Role Name"""
    if "->" not in role_names:
        await ctx.send("Usage: `!swaprole Old Role Name -> New Role Name`")
        return

    old_name, new_name = [part.strip() for part in role_names.split("->", 1)]
    old_role = discord.utils.get(ctx.guild.roles, name=old_name)
    new_role = discord.utils.get(ctx.guild.roles, name=new_name)

    if old_role is None:
        await ctx.send(f"Couldn't find a role named '{old_name}'.")
        return
    if new_role is None:
        await ctx.send(f"Couldn't find a role named '{new_name}'.")
        return

    await ctx.send(f"Swapping '{old_name}' -> '{new_name}' for all members... this may take a moment.")
    changed = 0
    errors = 0
    async for member in ctx.guild.fetch_members(limit=None):
        if old_role in member.roles:
            try:
                await member.remove_roles(old_role, reason=f"Bulk swap to {new_name}")
                await member.add_roles(new_role, reason=f"Bulk swap from {old_name}")
                changed += 1
            except Exception as e:
                errors += 1
                logger.error(f"Failed to swap role for {member}: {e}")

    await ctx.send(f"Done. Swapped {changed} member(s) from '{old_name}' to '{new_name}'. Errors: {errors}")


@bot.command(name="norole")
@commands.has_permissions(manage_roles=True)
async def norole(ctx):
    """Lists members with no roles at all (besides the default @everyone). Read-only, changes nothing."""
    await ctx.send("Checking for members with no roles... this may take a moment.")
    no_role_members = []
    async for member in ctx.guild.fetch_members(limit=None):
        if not member.bot and len(member.roles) <= 1:  # only @everyone
            no_role_members.append(f"{member} ({member.id})")

    if not no_role_members:
        await ctx.send("Everyone has at least one role.")
        return

    header = f"Found {len(no_role_members)} member(s) with no role:\n"
    body = "\n".join(no_role_members)
    full_message = header + body

    # Discord messages cap at 2000 chars - split into chunks if needed
    if len(full_message) <= 2000:
        await ctx.send(full_message)
    else:
        await ctx.send(header)
        chunk = ""
        for line in no_role_members:
            if len(chunk) + len(line) + 1 > 1900:
                await ctx.send(chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            await ctx.send(chunk)


@bot.command(name="backfillroles")
@commands.has_permissions(manage_roles=True)
async def backfillroles(ctx, *, role_name: str = None):
    """Usage: !backfillroles [Role Name] - defaults to NEW_MEMBER_ROLE_NAME if omitted"""
    target_name = role_name.strip() if role_name else NEW_MEMBER_ROLE_NAME
    role = discord.utils.get(ctx.guild.roles, name=target_name)
    if role is None:
        await ctx.send(f"Couldn't find a role named '{target_name}'.")
        return

    await ctx.send(f"Assigning '{target_name}' to all members missing it... this may take a moment.")
    assigned = 0
    async for member in ctx.guild.fetch_members(limit=None):
        if role not in member.roles and not member.bot:
            try:
                await member.add_roles(role, reason="Backfill - existing member missing role")
                assigned += 1
            except Exception as e:
                logger.error(f"Failed to assign role to {member}: {e}")

    await ctx.send(f"Done. Assigned '{target_name}' to {assigned} member(s) who didn't have it.")


@bot.command(name="checkvolumes")
@commands.has_permissions(manage_roles=True)
async def checkvolumes(ctx):
    await ctx.send(f"Running volume check now... ({'DRY RUN' if DRY_RUN else 'LIVE'})")
    results = await run_kick_check(triggered_by=f"manual by {ctx.author}")
    if "error" in results:
        await ctx.send(f"Error: {results['error']}")
        return
    await ctx.send(
        f"Done. Demoted: {len(results['kicked'])}, OK: {len(results['ok'])}, "
        f"Not due: {len(results['not_due'])}, Not found: {len(results['skipped_not_found'])}, "
        f"Errors: {len(results['errors'])}"
    )


@bot.event
async def on_member_join(member: discord.Member):
    role = discord.utils.get(member.guild.roles, name=NEW_MEMBER_ROLE_NAME)
    if role is None:
        logger.error(
            f"Could not assign role: no role named '{NEW_MEMBER_ROLE_NAME}' found in this server."
        )
        return
    try:
        await member.add_roles(role, reason="Auto-assigned on join")
        logger.info(f"Assigned '{NEW_MEMBER_ROLE_NAME}' role to new member {member}.")
    except discord.Forbidden:
        logger.error(
            f"Missing permission to assign '{NEW_MEMBER_ROLE_NAME}' to {member} - "
            f"make sure the bot's role is positioned ABOVE '{NEW_MEMBER_ROLE_NAME}' in Server Settings > Roles."
        )
    except Exception as e:
        logger.error(f"Failed to assign role to {member}: {e}")


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
