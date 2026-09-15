import os
import json
import uuid
import sqlite3
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set in environment")

DB_PATH = os.getenv("DB_PATH", "backups.db")
GUILD_ID = os.getenv("GUILD_ID")
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backupbot")

intents = discord.Intents.default()
intents.guilds = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS backups (
        id TEXT PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        data TEXT NOT NULL
    )""")
    conn.commit()
    return conn


def save_backup(guild_id, name, data):
    backup_id = str(uuid.uuid4())
    conn = db()
    try:
        conn.execute("INSERT INTO backups VALUES (?, ?, ?, ?, ?)",
                     (backup_id, guild_id, name, now_iso(), json.dumps(data)))
        conn.commit()
    finally:
        conn.close()
    return backup_id


def get_backup(guild_id, backup_id):
    conn = db()
    try:
        return conn.execute(
            "SELECT id, name, created_at, data FROM backups WHERE id=? AND guild_id=?",
            (backup_id, guild_id)
        ).fetchone()
    finally:
        conn.close()


def list_backups(guild_id):
    conn = db()
    try:
        return conn.execute(
            "SELECT id, name, created_at FROM backups WHERE guild_id=? ORDER BY created_at DESC",
            (guild_id,)
        ).fetchall()
    finally:
        conn.close()


def delete_backup(guild_id, backup_id):
    conn = db()
    try:
        cur = conn.execute("DELETE FROM backups WHERE id=? AND guild_id=?", (backup_id, guild_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def admin_only(interaction):
    return interaction.guild and interaction.user.guild_permissions.administrator


def role_data(role):
    return {
        "id": role.id,
        "name": role.name,
        "color": role.color.value,
        "permissions": role.permissions.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "position": role.position,
    }


def overwrite_data(channel):
    result = []
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Role):
            kind = "role"
        elif isinstance(target, discord.Member):
            kind = "member"
        else:
            continue
        allow, deny = overwrite.pair()
        result.append({
            "type": kind,
            "id": target.id,
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


def channel_kind(channel):
    if isinstance(channel, discord.TextChannel): return "text"
    if isinstance(channel, discord.VoiceChannel): return "voice"
    if isinstance(channel, discord.StageChannel): return "stage"
    if isinstance(channel, discord.ForumChannel): return "forum"
    return None


async def make_backup(guild, name):
    data = {
        "version": 3,
        "name": name,
        "created_at": now_iso(),
        "roles": [role_data(r) for r in guild.roles],
        "categories": [],
        "channels": [],
    }

    for category in guild.categories:
        data["categories"].append({
            "id": category.id,
            "name": category.name,
            "position": category.position,
            "overwrites": overwrite_data(category),
        })

    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue
        kind = channel_kind(channel)
        if not kind:
            continue
        item = {
            "id": channel.id,
            "name": channel.name,
            "type": kind,
            "category_id": channel.category_id,
            "position": channel.position,
            "overwrites": overwrite_data(channel),
        }
        if isinstance(channel, discord.TextChannel):
            item.update(topic=channel.topic, nsfw=channel.nsfw, slowmode_delay=channel.slowmode_delay)
        elif isinstance(channel, discord.VoiceChannel):
            item.update(bitrate=channel.bitrate, user_limit=channel.user_limit)
        elif isinstance(channel, discord.StageChannel):
            item.update(bitrate=channel.bitrate, user_limit=channel.user_limit)
        elif isinstance(channel, discord.ForumChannel):
            item.update(topic=channel.topic, nsfw=channel.nsfw, slowmode_delay=channel.slowmode_delay)
        data["channels"].append(item)
    return data


async def resolve_overwrites(guild, entries, role_map):
    result = {}
    for entry in entries:
        target = None
        old_id = int(entry["id"])
        if entry["type"] == "role":
            target = role_map.get(old_id)
            if old_id == guild.id:
                target = guild.default_role
        elif entry["type"] == "member":
            target = guild.get_member(old_id)
        if target is None:
            continue
        allow = discord.Permissions(int(entry.get("allow", 0)))
        deny = discord.Permissions(int(entry.get("deny", 0)))
        result[target] = discord.PermissionOverwrite.from_pair(allow, deny)
    return result


async def restore(guild, data):
    role_map = {guild.id: guild.default_role}
    roles_created = 0
    roles_skipped = 0

    for old in sorted(data.get("roles", []), key=lambda x: x.get("position", 0)):
        old_id = int(old["id"])
        if old_id == guild.id or old.get("name") == "@everyone":
            role_map[old_id] = guild.default_role
            continue
        try:
            role = await guild.create_role(
                name=str(old.get("name", "Restored Role"))[:100],
                permissions=discord.Permissions(int(old.get("permissions", 0))),
                colour=discord.Colour(int(old.get("color", 0))),
                hoist=bool(old.get("hoist", False)),
                mentionable=bool(old.get("mentionable", False)),
                reason="Discord server backup restore",
            )
            role_map[old_id] = role
            roles_created += 1
        except (discord.Forbidden, discord.HTTPException, discord.InvalidArgument) as e:
            roles_skipped += 1
            logger.warning("Role restore failed: %s", e)

    categories = {}
    categories_created = 0
    for old in sorted(data.get("categories", []), key=lambda x: x.get("position", 0)):
        try:
            overwrites = await resolve_overwrites(guild, old.get("overwrites", []), role_map)
            category = await guild.create_category(
                name=str(old.get("name", "Restored Category"))[:100],
                overwrites=overwrites,
                reason="Discord server backup restore",
            )
            categories[int(old["id"])] = category
            categories_created += 1
        except (discord.Forbidden, discord.HTTPException, discord.InvalidArgument) as e:
            logger.warning("Category restore failed: %s", e)

    channels_created = 0
    channels_skipped = 0
    for old in sorted(data.get("channels", []), key=lambda x: x.get("position", 0)):
        try:
            overwrites = await resolve_overwrites(guild, old.get("overwrites", []), role_map)
            category = categories.get(old.get("category_id"))
            name = str(old.get("name", "restored-channel"))[:100]
            kind = old.get("type")

            if kind == "text":
                await guild.create_text_channel(name=name, category=category,
                    topic=old.get("topic"), nsfw=bool(old.get("nsfw", False)),
                    slowmode_delay=int(old.get("slowmode_delay", 0)), overwrites=overwrites,
                    reason="Discord server backup restore")
            elif kind == "voice":
                await guild.create_voice_channel(name=name, category=category,
                    bitrate=int(old.get("bitrate", 64000)), user_limit=int(old.get("user_limit", 0)),
                    overwrites=overwrites, reason="Discord server backup restore")
            elif kind == "stage":
                await guild.create_stage_channel(name=name, category=category,
                    overwrites=overwrites, reason="Discord server backup restore")
            elif kind == "forum":
                await guild.create_forum(name=name, category=category,
                    topic=old.get("topic"), nsfw=bool(old.get("nsfw", False)),
                    slowmode_delay=int(old.get("slowmode_delay", 0)), overwrites=overwrites,
                    reason="Discord server backup restore")
            else:
                channels_skipped += 1
                continue
            channels_created += 1
        except (discord.Forbidden, discord.HTTPException, discord.InvalidArgument, TypeError, ValueError) as e:
            channels_skipped += 1
            logger.warning("Channel restore failed: %s", e)

    return roles_created, roles_skipped, categories_created, channels_created, channels_skipped


@bot.event
async def on_ready():
    db().close()
    logger.info("Logged in as %s (%s)", bot.user, bot.user.id)
    logger.info("Connected to %d guild(s)", len(bot.guilds))

    try:
        # Global sync makes commands available everywhere the bot is installed.
        synced = await bot.tree.sync()
        logger.info("Global slash-command sync complete: %d command(s)", len(synced))

        # Guild sync makes commands appear immediately in the test/server guild.
        if GUILD_ID:
            guild = bot.get_guild(int(GUILD_ID))
            if guild:
                bot.tree.copy_global_to(guild=guild)
                guild_synced = await bot.tree.sync(guild=guild)
                logger.info("Guild slash-command sync complete for %s: %d command(s)", guild.name, len(guild_synced))
            else:
                logger.warning("GUILD_ID=%s was not found in the bot's guilds", GUILD_ID)
    except Exception:
        logger.exception("Slash command sync failed")


@bot.tree.command(name="backup", description="Create a backup of this server")
@app_commands.describe(name="Name for the backup")
async def backup_command(interaction: discord.Interaction, name: str = "Server Backup"):
    if not admin_only(interaction):
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    data = await make_backup(interaction.guild, name)
    backup_id = save_backup(interaction.guild.id, name, data)
    embed = discord.Embed(title="Backup Created", colour=discord.Colour.green())
    embed.add_field(name="Name", value=name, inline=False)
    embed.add_field(name="Backup ID", value=f"`{backup_id}`", inline=False)
    embed.add_field(name="Roles", value=len(data["roles"]))
    embed.add_field(name="Categories", value=len(data["categories"]))
    embed.add_field(name="Channels", value=len(data["channels"]))
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="backups", description="List this server's backups")
async def backups_command(interaction: discord.Interaction):
    if not admin_only(interaction):
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    rows = list_backups(interaction.guild.id)
    if not rows:
        await interaction.response.send_message("No backups found.", ephemeral=True)
        return
    text = "\n".join(f"`{r[0]}` — **{r[1]}** — {r[2]}" for r in rows[:20])
    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="restore", description="Restore a server backup")
@app_commands.describe(backup_id="The backup ID from /backups")
async def restore_command(interaction: discord.Interaction, backup_id: str):
    if not admin_only(interaction):
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    row = get_backup(interaction.guild.id, backup_id.strip())
    if not row:
        await interaction.response.send_message("Backup not found in this server.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    _, name, created_at, data_json = row
    data = json.loads(data_json)
    result = await restore(interaction.guild, data)
    roles_created, roles_skipped, categories_created, channels_created, channels_skipped = result
    embed = discord.Embed(title="Restore Complete", colour=discord.Colour.green())
    embed.add_field(name="Backup", value=name, inline=False)
    embed.add_field(name="Roles", value=f"{roles_created} created / {roles_skipped} skipped")
    embed.add_field(name="Categories", value=str(categories_created))
    embed.add_field(name="Channels", value=f"{channels_created} created / {channels_skipped} skipped")
    embed.set_footer(text=f"Backup created {created_at}. Existing server items were not deleted.")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="deletebackup", description="Delete one of this server's backups")
@app_commands.describe(backup_id="Backup ID to delete")
async def deletebackup_command(interaction: discord.Interaction, backup_id: str):
    if not admin_only(interaction):
        await interaction.response.send_message("Administrator permission required.", ephemeral=True)
        return
    if delete_backup(interaction.guild.id, backup_id.strip()):
        await interaction.response.send_message("Backup deleted.", ephemeral=True)
    else:
        await interaction.response.send_message("Backup not found.", ephemeral=True)


@bot.command(name="backup")
@commands.has_guild_permissions(administrator=True)
async def prefix_backup(ctx, *, name: str = "Server Backup"):
    data = await make_backup(ctx.guild, name)
    backup_id = save_backup(ctx.guild.id, name, data)
    await ctx.send(f"Backup created: `{backup_id}`")


@bot.command(name="backups")
@commands.has_guild_permissions(administrator=True)
async def prefix_backups(ctx):
    rows = list_backups(ctx.guild.id)
    if not rows:
        await ctx.send("No backups found.")
        return
    await ctx.send("\n".join(f"`{r[0]}` — {r[1]}" for r in rows[:20]))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Administrator permission required.", delete_after=8)
    elif not isinstance(error, commands.CommandNotFound):
        logger.error("Command error: %s", error)


if __name__ == "__main__":
    db().close()
    bot.run(TOKEN)
