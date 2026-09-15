import os
import json
import uuid
import sqlite3
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands, Interaction, Embed, Colour, ButtonStyle
from discord.ui import View, Button
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set in environment")

DB_PATH = os.getenv("DB_PATH", "backups.db")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backupbot")

# These are the only intents this bot actually needs.
# There are NO guild_role_create/guild_channel_create/etc. intents in discord.py.
intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="/", intents=intents)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backups (
                id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_backup(guild_id: int, name: str, data: dict) -> str:
    backup_id = str(uuid.uuid4())
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO backups (id, guild_id, name, created_at, data) VALUES (?, ?, ?, ?, ?)",
            (backup_id, guild_id, name, now_iso(), json.dumps(data)),
        )
        conn.commit()
    finally:
        conn.close()
    return backup_id


def get_backups(guild_id: int):
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT id, name, created_at FROM backups WHERE guild_id=? ORDER BY created_at DESC",
            (guild_id,),
        ).fetchall()
    finally:
        conn.close()


def get_backup(backup_id: str, guild_id: int | None = None):
    conn = sqlite3.connect(DB_PATH)
    try:
        if guild_id is None:
            return conn.execute(
                "SELECT id, guild_id, name, created_at, data FROM backups WHERE id=?",
                (backup_id,),
            ).fetchone()
        return conn.execute(
            "SELECT id, guild_id, name, created_at, data FROM backups WHERE id=? AND guild_id=?",
            (backup_id, guild_id),
        ).fetchone()
    finally:
        conn.close()


def delete_backup(backup_id: str, guild_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.execute(
            "DELETE FROM backups WHERE id=? AND guild_id=?",
            (backup_id, guild_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


async def is_admin_user(interaction: Interaction) -> bool:
    if not interaction.guild:
        return False
    if await bot.is_owner(interaction.user):
        return True
    return interaction.user.guild_permissions.administrator


def is_admin():
    async def predicate(interaction: Interaction):
        return await is_admin_user(interaction)

    return app_commands.checks.check(predicate)


def serialize_overwrites(channel) -> list[dict]:
    result = []
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Role):
            target_type = "role"
        elif isinstance(target, discord.Member):
            target_type = "member"
        else:
            continue

        allow, deny = overwrite.pair()
        result.append(
            {
                "target_type": target_type,
                "target_id": target.id,
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    return result


def serialize_role(role: discord.Role) -> dict:
    return {
        "id": role.id,
        "name": role.name,
        "color": role.color.value,
        "permissions": role.permissions.value,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "position": role.position,
        # Role icons can be assets/emojis and are not safely JSON serializable.
        # They are intentionally omitted so backup creation cannot fail.
    }


def channel_type(channel) -> str:
    if isinstance(channel, discord.TextChannel):
        return "text"
    if isinstance(channel, discord.VoiceChannel):
        return "voice"
    if isinstance(channel, discord.StageChannel):
        return "stage"
    if isinstance(channel, discord.ForumChannel):
        return "forum"
    return "unsupported"


async def create_backup(interaction: Interaction, name: str):
    if not await is_admin_user(interaction):
        await interaction.followup.send("Only server administrators can create backups.", ephemeral=True)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
        return

    await interaction.followup.send("Creating backup...", ephemeral=True)

    data = {
        "version": 2,
        "name": name,
        "created_at": now_iso(),
        "roles": [],
        "categories": [],
        "channels": [],
        "settings": {
            "name": guild.name,
            "description": guild.description,
            "verification_level": int(guild.verification_level.value),
            "default_notifications": int(guild.default_notifications.value),
            "explicit_content_filter": int(guild.explicit_content_filter.value),
        },
    }

    for role in guild.roles:
        data["roles"].append(serialize_role(role))

    for category in guild.categories:
        data["categories"].append(
            {
                "id": category.id,
                "name": category.name,
                "position": category.position,
                "permission_overwrites": serialize_overwrites(category),
            }
        )

    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue

        kind = channel_type(channel)
        if kind == "unsupported":
            logger.info("Skipping unsupported channel type: %s (%s)", channel.name, type(channel).__name__)
            continue

        item = {
            "id": channel.id,
            "name": channel.name,
            "type": kind,
            "category_id": channel.category_id,
            "position": channel.position,
            "permission_overwrites": serialize_overwrites(channel),
        }

        if isinstance(channel, discord.TextChannel):
            item.update(
                {
                    "topic": channel.topic,
                    "nsfw": channel.nsfw,
                    "slowmode_delay": channel.slowmode_delay,
                }
            )
        elif isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            item.update(
                {
                    "bitrate": channel.bitrate,
                    "user_limit": channel.user_limit,
                }
            )
        elif isinstance(channel, discord.ForumChannel):
            item.update(
                {
                    "topic": channel.topic,
                    "nsfw": channel.nsfw,
                    "slowmode_delay": channel.slowmode_delay,
                }
            )

        data["channels"].append(item)

    backup_id = save_backup(guild.id, name, data)

    embed = Embed(title="Backup Created", colour=Colour.green())
    embed.add_field(name="ID", value=f"`{backup_id}`", inline=False)
    embed.add_field(name="Name", value=name, inline=False)
    embed.add_field(name="Roles", value=str(len(data["roles"])), inline=True)
    embed.add_field(name="Categories", value=str(len(data["categories"])), inline=True)
    embed.add_field(name="Channels", value=str(len(data["channels"])), inline=True)
    embed.add_field(name="Created", value=now_iso(), inline=False)

    await interaction.edit_original_response(embed=embed)


async def resolve_member(guild: discord.Guild, member_id: int):
    member = guild.get_member(member_id)
    if member:
        return member
    try:
        return await guild.fetch_member(member_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


async def build_overwrites(guild: discord.Guild, entries: list[dict], role_map: dict[int, discord.Role]):
    overwrites = {}

    for entry in entries:
        target = None
        target_id = int(entry["target_id"])

        if entry["target_type"] == "role":
            target = role_map.get(target_id)
            if target is None and target_id == guild.id:
                target = guild.default_role
        elif entry["target_type"] == "member":
            target = await resolve_member(guild, target_id)

        if target is None:
            continue

        allow = discord.Permissions(int(entry.get("allow", 0)))
        deny = discord.Permissions(int(entry.get("deny", 0)))
        overwrites[target] = discord.PermissionOverwrite.from_pair(allow, deny)

    return overwrites


async def restore_backup(interaction: Interaction, backup_id: str):
    guild = interaction.guild
    if guild is None:
        await interaction.edit_original_response(content="This command can only be used in a server.")
        return

    row = get_backup(backup_id, guild.id)
    if not row:
        await interaction.edit_original_response(content="Backup not found in this server.")
        return

    _, _, name, created_at, data_json = row
    data = json.loads(data_json)

    await interaction.edit_original_response(content="Restoring backup... This may take a while.")

    role_map: dict[int, discord.Role] = {guild.id: guild.default_role}
    created_roles = 0
    skipped_roles = 0

    # Restore roles first so channel/category permission overwrites can reference them.
    # @everyone and managed/integration roles cannot be recreated.
    for role_data in sorted(data.get("roles", []), key=lambda r: r.get("position", 0)):
        old_id = int(role_data["id"])

        if old_id == guild.id or role_data["name"] == "@everyone":
            role_map[old_id] = guild.default_role
            continue

        try:
            permissions = discord.Permissions(int(role_data.get("permissions", 0)))
            color = discord.Colour(int(role_data.get("color", 0)))

            role = await guild.create_role(
                name=str(role_data.get("name", "Restored Role"))[:100],
                permissions=permissions,
                colour=color,
                hoist=bool(role_data.get("hoist", False)),
                mentionable=bool(role_data.get("mentionable", False)),
                reason=f"Restore backup {backup_id}",
            )
            role_map[old_id] = role
            created_roles += 1
        except (discord.Forbidden, discord.HTTPException, discord.InvalidArgument) as exc:
            skipped_roles += 1
            logger.warning("Could not restore role %s: %s", role_data.get("name"), exc)

    # Restore role positions after all roles exist.
    for role_data in sorted(data.get("roles", []), key=lambda r: r.get("position", 0)):
        role = role_map.get(int(role_data["id"]))
        if not role or role.is_default():
            continue
        try:
            position = max(1, min(int(role_data.get("position", 1)), guild.me.top_role.position - 1))
            await role.edit(position=position, reason=f"Restore backup {backup_id}")
        except (discord.Forbidden, discord.HTTPException, discord.InvalidArgument):
            pass

    category_map: dict[int, discord.CategoryChannel] = {}
    created_categories = 0

    # Categories first.
    for category_data in sorted(data.get("categories", []), key=lambda c: c.get("position", 0)):
        try:
            overwrites = await build_overwrites(
                guild,
                category_data.get("permission_overwrites", []),
                role_map,
            )
            category = await guild.create_category(
                name=str(category_data.get("name", "Restored Category"))[:100],
                overwrites=overwrites,
                reason=f"Restore backup {backup_id}",
            )
            category_map[int(category_data["id"])] = category
            created_categories += 1
        except (discord.Forbidden, discord.HTTPException, discord.InvalidArgument) as exc:
            logger.warning("Could not restore category %s: %s", category_data.get("name"), exc)

    created_channels = 0
    skipped_channels = 0

    # Channels are restored after categories.
    for channel_data in sorted(data.get("channels", []), key=lambda c: c.get("position", 0)):
        try:
            overwrites = await build_overwrites(
                guild,
                channel_data.get("permission_overwrites", []),
                role_map,
            )

            category = category_map.get(channel_data.get("category_id"))
            name_value = str(channel_data.get("name", "restored-channel"))[:100]
            kind = channel_data.get("type")

            if kind == "text":
                await guild.create_text_channel(
                    name=name_value,
                    category=category,
                    topic=channel_data.get("topic"),
                    nsfw=bool(channel_data.get("nsfw", False)),
                    slowmode_delay=int(channel_data.get("slowmode_delay", 0)),
                    overwrites=overwrites,
                    reason=f"Restore backup {backup_id}",
                )
            elif kind == "voice":
                await guild.create_voice_channel(
                    name=name_value,
                    category=category,
                    bitrate=int(channel_data.get("bitrate", 64000)),
                    user_limit=int(channel_data.get("user_limit", 0)),
                    overwrites=overwrites,
                    reason=f"Restore backup {backup_id}",
                )
            elif kind == "stage":
                await guild.create_stage_channel(
                    name=name_value,
                    category=category,
                    overwrites=overwrites,
                    reason=f"Restore backup {backup_id}",
                )
            elif kind == "forum":
                await guild.create_forum(
                    name=name_value,
                    category=category,
                    topic=channel_data.get("topic"),
                    nsfw=bool(channel_data.get("nsfw", False)),
                    slowmode_delay=int(channel_data.get("slowmode_delay", 0)),
                    overwrites=overwrites,
                    reason=f"Restore backup {backup_id}",
                )
            else:
                skipped_channels += 1
                continue

            created_channels += 1
        except (discord.Forbidden, discord.HTTPException, discord.InvalidArgument, TypeError, ValueError) as exc:
            skipped_channels += 1
            logger.warning("Could not restore channel %s: %s", channel_data.get("name"), exc)

    embed = Embed(title="Restore Complete", colour=Colour.green())
    embed.add_field(name="Backup", value=f"{name} (`{backup_id}`)", inline=False)
    embed.add_field(name="Created Roles", value=str(created_roles), inline=True)
    embed.add_field(name="Skipped Roles", value=str(skipped_roles), inline=True)
    embed.add_field(name="Categories", value=str(created_categories), inline=True)
    embed.add_field(name="Channels", value=str(created_channels), inline=True)
    embed.add_field(name="Skipped Channels", value=str(skipped_channels), inline=True)
    embed.add_field(name="Created", value=created_at, inline=False)
    embed.set_footer(text="Existing channels and roles were not deleted.")

    await interaction.edit_original_response(content=None, embed=embed, view=None)


class ConfirmView(View):
    def __init__(self, backup_id: str, action: str, guild_id: int):
        super().__init__(timeout=180)
        self.backup_id = backup_id
        self.action = action
        self.guild_id = guild_id

    @discord.ui.button(label="Confirm", style=ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, button: Button):
        if not await is_admin_user(interaction):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return

        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This confirmation belongs to another server.", ephemeral=True)
            return

        row = get_backup(self.backup_id, self.guild_id)
        if not row:
            await interaction.response.send_message("Backup not found in this server.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            if self.action == "restore":
                await restore_backup(interaction, self.backup_id)
            elif self.action == "delete":
                if delete_backup(self.backup_id, self.guild_id):
                    await interaction.edit_original_response(
                        content=f"Backup `{self.backup_id}` deleted.",
                        embed=None,
                        view=None,
                    )
                else:
                    await interaction.edit_original_response(content="Backup was not found.", view=None)
        except Exception as exc:
            logger.exception("Error during %s", self.action)
            await interaction.edit_original_response(content=f"An error occurred: {exc}", view=None)

    @discord.ui.button(label="Cancel", style=ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: Button):
        await interaction.response.edit_message(view=None)
        self.stop()


class BackupCommands(app_commands.Group):
    def __init__(self):
        super().__init__(name="backup", description="Server backup management")

    @app_commands.command(name="create", description="Create a backup of the server")
    @app_commands.describe(name="Name for the backup")
    @is_admin()
    async def create(self, interaction: Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        await create_backup(interaction, name.strip() or "Server Backup")

    @app_commands.command(name="list", description="List saved backups")
    async def list(self, interaction: Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        backups = get_backups(interaction.guild.id)
        if not backups:
            await interaction.response.send_message("No backups found.", ephemeral=True)
            return

        embed = Embed(title="Saved Backups", colour=Colour.blurple())
        for backup_id, backup_name, created in backups[:25]:
            embed.add_field(
                name=backup_name[:256],
                value=f"ID: `{backup_id}`\nCreated: {created}",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="restore", description="Restore a backup")
    @app_commands.describe(backup="Backup ID")
    @is_admin()
    async def restore(self, interaction: Interaction, backup: str):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        row = get_backup(backup.strip(), interaction.guild.id)
        if not row:
            await interaction.response.send_message("Backup not found in this server.", ephemeral=True)
            return

        _, _, name, created_at, _ = row
        embed = Embed(title="Restore Confirmation", colour=Colour.orange())
        embed.add_field(name="Backup", value=f"{name} (`{backup}`)", inline=False)
        embed.add_field(name="Created", value=created_at, inline=False)
        embed.set_footer(text="Existing channels and roles will NOT be deleted.")
        await interaction.response.send_message(
            embed=embed,
            view=ConfirmView(backup.strip(), "restore", interaction.guild.id),
            ephemeral=True,
        )

    @app_commands.command(name="delete", description="Delete a backup")
    @app_commands.describe(backup="Backup ID")
    @is_admin()
    async def delete(self, interaction: Interaction, backup: str):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        row = get_backup(backup.strip(), interaction.guild.id)
        if not row:
            await interaction.response.send_message("Backup not found in this server.", ephemeral=True)
            return

        _, _, name, created_at, _ = row
        embed = Embed(title="Delete Confirmation", colour=Colour.red())
        embed.add_field(name="Backup", value=f"{name} (`{backup}`)", inline=False)
        embed.add_field(name="Created", value=created_at, inline=False)
        await interaction.response.send_message(
            embed=embed,
            view=ConfirmView(backup.strip(), "delete", interaction.guild.id),
            ephemeral=True,
        )

    @app_commands.command(name="info", description="Show backup details")
    @app_commands.describe(backup="Backup ID")
    async def info(self, interaction: Interaction, backup: str):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        row = get_backup(backup.strip(), interaction.guild.id)
        if not row:
            await interaction.response.send_message("Backup not found in this server.", ephemeral=True)
            return

        _, _, name, created_at, data_json = row
        data = json.loads(data_json)
        embed = Embed(title=f"Backup Info: {name}", colour=Colour.blurple())
        embed.add_field(name="ID", value=f"`{backup}`", inline=False)
        embed.add_field(name="Created", value=created_at, inline=False)
        embed.add_field(name="Roles", value=str(len(data.get("roles", []))), inline=True)
        embed.add_field(name="Categories", value=str(len(data.get("categories", []))), inline=True)
        embed.add_field(name="Channels", value=str(len(data.get("channels", []))), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# Add the command group once. Doing this inside on_ready can add it again after reconnects.
bot.tree.add_command(BackupCommands())


@bot.tree.command(name="help", description="Show help information")
async def help_command(interaction: Interaction):
    embed = Embed(title="Backup Bot Help", colour=Colour.blurple())
    embed.add_field(name="/backup create", value="Create a new server backup", inline=False)
    embed.add_field(name="/backup list", value="List saved backups", inline=False)
    embed.add_field(name="/backup restore", value="Restore a backup by ID", inline=False)
    embed.add_field(name="/backup delete", value="Delete a backup by ID", inline=False)
    embed.add_field(name="/backup info", value="Show backup details", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    init_db()
    if not getattr(bot, "_commands_synced", False):
        await bot.tree.sync()
        bot._commands_synced = True
    logger.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")


@bot.event
async def on_app_command_error(interaction: Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        message = "You need Administrator permission to use this command."
    else:
        logger.exception("Application command error", exc_info=error)
        message = "Something went wrong while running that command."

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN)
