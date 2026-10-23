# Trace: Mayhem's moderation bot.
# /ban and /unban ban players from alpha through Roblox's Ban API (Open Cloud). In-game bans
# (BanAsync) go to the same place, so a ban from Discord and a ban from the game are the same ban.

import asyncio
import os
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()  # reads the .env file in this folder
for name in ["DISCORD_TOKEN", "ROBLOX_API_KEY", "ROBLOX_UNIVERSE_ID", "BAN_ROLE_ID", "GUILD_ID"]:
    if not os.getenv(name):
        raise SystemExit(f"{name} is missing from .env")

TOKEN = os.getenv("DISCORD_TOKEN")
ROBLOX_API_KEY = os.getenv("ROBLOX_API_KEY")
UNIVERSE_ID = os.getenv("ROBLOX_UNIVERSE_ID")
BAN_ROLE_ID = int(os.getenv("BAN_ROLE_ID"))
GUILD_ID = int(os.getenv("GUILD_ID"))
LOG_WEBHOOK_URL = os.getenv("LOG_WEBHOOK_URL")  # optional: where bans and unbans are logged

GROUP_ID = 301273641  # Mayhemic Games
PROTECTED_RANK = 4  # Moderator and up can't be banned from Discord (same rule as in-game)

# Same lengths as the in-game duration chips. 0 = permanent.
DURATIONS = [
    app_commands.Choice(name="1 Hour", value=3600),
    app_commands.Choice(name="6 Hours", value=21600),
    app_commands.Choice(name="12 Hours", value=43200),
    app_commands.Choice(name="1 Day", value=86400),
    app_commands.Choice(name="3 Days", value=259200),
    app_commands.Choice(name="7 Days", value=604800),
    app_commands.Choice(name="14 Days", value=1209600),
    app_commands.Choice(name="30 Days", value=2592000),
    app_commands.Choice(name="Permanent", value=0),
]

BANS_PER_PAGE = 20  # /banlist

# Open Cloud's Ban API for alpha. The API key goes in a header on every request.
RESTRICTIONS_URL = f"https://apis.roblox.com/cloud/v2/universes/{UNIVERSE_ID}/user-restrictions"
API_HEADERS = {"x-api-key": ROBLOX_API_KEY}

GUILD = discord.Object(id=GUILD_ID)  # the bot's home server; its commands only exist there
TIMEOUT = aiohttp.ClientTimeout(total=15)  # give up on Roblox after 15 seconds

client = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(client)  # holds the bot's slash commands


# ---------------------------------------------------------------- talking to Roblox

class RobloxError(Exception):
    """Something went wrong talking to Roblox. The message is shown to staff."""


async def find_user(session, username):
    """Turns a Roblox username into {"id", "name", "displayName"}."""
    url = "https://users.roblox.com/v1/usernames/users"
    async with session.post(url, json={"usernames": [username], "excludeBannedUsers": False}) as response:
        if response.status != 200:
            raise RobloxError(f"Roblox user lookup failed ({response.status})")
        users = (await response.json())["data"]
    if not users:
        raise RobloxError(f"There's no Roblox user called **{username}**")
    return users[0]


async def group_rank(session, user_id):
    """Their rank in Mayhemic Games (0 if they aren't in it)."""
    url = f"https://groups.roblox.com/v1/users/{user_id}/groups/roles"
    async with session.get(url) as response:
        if response.status != 200:
            raise RobloxError(f"Couldn't check their group rank ({response.status})")
        for membership in (await response.json())["data"]:
            if membership["group"]["id"] == GROUP_ID:
                return membership["role"]["rank"]
    return 0


async def names_for(session, user_ids):
    """{user_id: {"name", "displayName"}} for up to 100 players. Missing players are just left out."""
    url = "https://users.roblox.com/v1/users"
    async with session.post(url, json={"userIds": user_ids, "excludeBannedUsers": False}) as response:
        if response.status != 200:
            return {}
        return {user["id"]: user for user in (await response.json())["data"]}


async def set_ban(session, user_id, restriction):
    """Writes the player's game-join restriction (their ban) for alpha through Open Cloud."""
    async with session.patch(
        f"{RESTRICTIONS_URL}/{user_id}",
        params={"updateMask": "gameJoinRestriction"},
        headers=API_HEADERS,
        json={"gameJoinRestriction": restriction},
    ) as response:
        if response.status != 200:
            details = await response.text()
            raise RobloxError(f"Roblox said no ({response.status}): {details[:300]}")


async def get_ban(session, user_id):
    """The player's game-join restriction, or None if they've never had one."""
    async with session.get(f"{RESTRICTIONS_URL}/{user_id}", headers=API_HEADERS) as response:
        if response.status == 404:
            return None
        if response.status != 200:
            details = await response.text()
            raise RobloxError(f"Couldn't read their ban ({response.status}): {details[:300]}")
        return (await response.json()).get("gameJoinRestriction")


async def list_bans(session):
    """[(user_id, restriction)] for everyone banned from alpha right now."""
    bans = []
    page_token = ""
    for _ in range(20):  # Roblox sends 100 at a time; stop at 2,000 so a bad reply can't loop forever
        params = {"maxPageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        async with session.get(RESTRICTIONS_URL, headers=API_HEADERS, params=params) as response:
            if response.status != 200:
                details = await response.text()
                raise RobloxError(f"Couldn't read the ban list ({response.status}): {details[:300]}")
            data = await response.json()
        for entry in data.get("userRestrictions", []):
            restriction = entry.get("gameJoinRestriction")
            if is_banned(restriction):
                user_id = int(entry["user"].split("/")[-1])  # "users/123" -> 123
                bans.append((user_id, restriction))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return bans


# ---------------------------------------------------------------- reading a ban

def parse_time(text):
    """Open Cloud times look like 2026-10-23T15:04:05.123Z. Returns None if there isn't one."""
    if not text:
        return None
    whole_seconds = text.rstrip("Z").split(".")[0]
    return datetime.strptime(whole_seconds, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def ban_ends(restriction):
    """When the ban runs out, or None if it's permanent."""
    start = parse_time(restriction.get("startTime"))
    duration = restriction.get("duration")  # e.g. "86400s"; missing = permanent
    if not start or not duration:
        return None
    return start + timedelta(seconds=float(duration.rstrip("s")))


def is_banned(restriction):
    """True if this restriction is on and hasn't run out yet."""
    if not restriction or not restriction.get("active"):
        return False
    ends = ban_ends(restriction)
    return ends is None or ends > datetime.now(timezone.utc)


def when(moment):
    """A Discord timestamp: shows in each reader's own time zone, e.g. "in 3 days"."""
    return f"<t:{int(moment.timestamp())}:R>"


# ---------------------------------------------------------------- ban log

def profile_link(user):
    return f"[{user['displayName']} (@{user['name']})](https://www.roblox.com/users/{user['id']}/profile)"


async def post_log(session, embed):
    """Posts an embed to the log channel's webhook. Returns False if it couldn't (the ban still happened)."""
    if not LOG_WEBHOOK_URL:
        return True
    webhook = discord.Webhook.from_url(LOG_WEBHOOK_URL, session=session)
    try:
        await webhook.send(embed=embed, username="Trace", allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.HTTPException as error:
        print(f"Couldn't post to the log webhook: {error}")
        return False


# ---------------------------------------------------------------- permissions

def can_ban(interaction):
    """Only people with the ban role, in the bot's home server."""
    if interaction.guild_id != GUILD_ID or not isinstance(interaction.user, discord.Member):
        return False
    return any(role.id == BAN_ROLE_ID for role in interaction.user.roles)


# ---------------------------------------------------------------- commands

@tree.command(guild=GUILD, description="Check that the bot is online")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(client.latency * 1000)} ms", ephemeral=True)


@tree.command(guild=GUILD, description="Ban a player from alpha")
@app_commands.describe(
    username="Their Roblox username",
    duration="How long the ban lasts",
    reason="Shown to them when they try to join",
)
@app_commands.choices(duration=DURATIONS)
async def ban(
    interaction: discord.Interaction,
    username: str,
    duration: app_commands.Choice[int],
    reason: app_commands.Range[str, 1, 400],
):
    if not can_ban(interaction):
        await interaction.response.send_message("You don't have permission to ban.", ephemeral=True)
        return
    await interaction.response.defer()  # Roblox can take a few seconds; shows "Trace is thinking..."

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            user = await find_user(session, username)
            if await group_rank(session, user["id"]) >= PROTECTED_RANK:
                await interaction.followup.send(f"**{user['name']}** is staff in the group and can't be banned from Discord.")
                return
            restriction = {
                "active": True,
                "displayReason": reason,
                "privateReason": f"Banned from Discord by {interaction.user} ({interaction.user.id}): {reason}"[:1000],
                "excludeAltAccounts": False,  # Roblox also bans accounts it thinks are their alts
            }
            if duration.value > 0:
                restriction["duration"] = f"{duration.value}s"  # left out = permanent
            await set_ban(session, user["id"], restriction)

            log = discord.Embed(title="Player banned", color=discord.Color.red(), timestamp=discord.utils.utcnow())
            log.add_field(name="Player", value=profile_link(user), inline=False)
            log.add_field(name="Duration", value=duration.name)
            log.add_field(name="Banned by", value=f"{interaction.user.display_name} (@{interaction.user.name})")
            log.add_field(name="Reason", value=reason, inline=False)
            log.set_footer(text="From Discord")
            logged = await post_log(session, log)
    except (RobloxError, aiohttp.ClientError, asyncio.TimeoutError) as error:
        await interaction.followup.send(f"Ban failed: {error or 'Roblox took too long to answer'}")
        return

    await interaction.followup.send(
        f"Banned **{user['displayName']}** (@{user['name']}) from alpha for **{duration.name}**.\nReason: {reason}"
        + ("" if logged else "\n(Couldn't post this to the log channel.)")
    )


@tree.command(guild=GUILD, description="Lift a player's ban from alpha")
@app_commands.describe(username="Their Roblox username")
async def unban(interaction: discord.Interaction, username: str):
    if not can_ban(interaction):
        await interaction.response.send_message("You don't have permission to unban.", ephemeral=True)
        return
    await interaction.response.defer()

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            user = await find_user(session, username)
            await set_ban(session, user["id"], {"active": False})

            log = discord.Embed(title="Player unbanned", color=discord.Color.green(), timestamp=discord.utils.utcnow())
            log.add_field(name="Player", value=profile_link(user), inline=False)
            log.add_field(name="Unbanned by", value=f"{interaction.user.display_name} (@{interaction.user.name})")
            log.set_footer(text="From Discord")
            logged = await post_log(session, log)
    except (RobloxError, aiohttp.ClientError, asyncio.TimeoutError) as error:
        await interaction.followup.send(f"Unban failed: {error or 'Roblox took too long to answer'}")
        return

    await interaction.followup.send(
        f"Unbanned **{user['displayName']}** (@{user['name']}) from alpha."
        + ("" if logged else "\n(Couldn't post this to the log channel.)")
    )


@tree.command(guild=GUILD, description="Check whether a player is banned from alpha")
@app_commands.describe(username="Their Roblox username")
async def checkban(interaction: discord.Interaction, username: str):
    if not can_ban(interaction):
        await interaction.response.send_message("You don't have permission to check bans.", ephemeral=True)
        return
    await interaction.response.defer()

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            user = await find_user(session, username)
            restriction = await get_ban(session, user["id"])
    except (RobloxError, aiohttp.ClientError, asyncio.TimeoutError) as error:
        await interaction.followup.send(f"Check failed: {error or 'Roblox took too long to answer'}")
        return

    if not is_banned(restriction):
        await interaction.followup.send(f"**{user['displayName']}** (@{user['name']}) isn't banned from alpha.")
        return

    ends = ban_ends(restriction)
    started = parse_time(restriction.get("startTime"))
    embed = discord.Embed(title="Banned from alpha", color=discord.Color.orange())
    embed.add_field(name="Player", value=profile_link(user), inline=False)
    embed.add_field(name="Ends", value=when(ends) if ends else "Never (permanent)")
    embed.add_field(name="Banned", value=when(started) if started else "Unknown")
    embed.add_field(name="Reason they see", value=restriction.get("displayReason") or "None", inline=False)
    embed.add_field(name="Staff note", value=restriction.get("privateReason") or "None", inline=False)
    await interaction.followup.send(embed=embed)


@tree.command(guild=GUILD, description="List everyone banned from alpha")
@app_commands.describe(page=f"Which page ({BANS_PER_PAGE} bans per page)")
async def banlist(interaction: discord.Interaction, page: app_commands.Range[int, 1, 100] = 1):
    if not can_ban(interaction):
        await interaction.response.send_message("You don't have permission to see the ban list.", ephemeral=True)
        return
    await interaction.response.defer()

    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            bans = await list_bans(session)
            # Newest bans first
            bans.sort(key=lambda ban: parse_time(ban[1].get("startTime")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            pages = max(1, -(-len(bans) // BANS_PER_PAGE))  # round up
            page = min(page, pages)
            shown = bans[(page - 1) * BANS_PER_PAGE : page * BANS_PER_PAGE]
            names = await names_for(session, [user_id for user_id, _ in shown])
    except (RobloxError, aiohttp.ClientError, asyncio.TimeoutError) as error:
        await interaction.followup.send(f"Couldn't load the ban list: {error or 'Roblox took too long to answer'}")
        return

    if not bans:
        await interaction.followup.send("Nobody is banned from alpha right now.")
        return

    lines = []
    for user_id, restriction in shown:
        user = names.get(user_id)
        who = f"**{user['displayName']}** (@{user['name']})" if user else f"User {user_id}"
        ends = ban_ends(restriction)
        length = f"ends {when(ends)}" if ends else "permanent"
        reason = (restriction.get("displayReason") or "no reason")[:60]
        lines.append(f"{who} · {length} · {reason}")

    embed = discord.Embed(title=f"Banned from alpha ({len(bans)})", description="\n".join(lines), color=discord.Color.orange())
    embed.set_footer(text=f"Page {page} of {pages}")
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------- start up

synced = False


@client.event
async def on_ready():
    global synced
    if not synced:  # on_ready can run again after a reconnect; only sync once
        tree.clear_commands(guild=None)
        await tree.sync()  # removes the old server-wide /ping from the first test
        await tree.sync(guild=GUILD)  # registers this file's commands in the home server (shows up instantly)
        synced = True
    print(f"Logged in as {client.user}")


client.run(TOKEN)
