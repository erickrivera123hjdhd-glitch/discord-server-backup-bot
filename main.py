import os
import logging
import sqlite3
import asyncio
import uuid
import json
from datetime import datetime

import discord
from discord import app_commands, Interaction, Embed, Colour, ButtonStyle, Locale
from discord.ui import View, Button, Modal, TextInput
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set in environment")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("backupbot")

intents = discord.Intents.default()
intents.guilds = True
intents.guild_messages = True
intents.guild_role_create = True
intents.guild_role_delete = True
intents.guild_channel_create = True
intents.guild_channel_delete = True
intents.guild_channel_update = True
intents.guild_members = True

bot = commands.Bot(command_prefix="/", intents=intents)

DB_PATH = os.getenv("DB_PATH", "backups.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS backups (
            id TEXT PRIMARY KEY,
            guild_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            data TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_backup(guild_id, name, data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    backup_id = str(uuid.uuid4())
    c.execute(
        "INSERT INTO backups (id, guild_id, name, created_at, data) VALUES (?, ?, ?, ?, ?)",
        (backup_id, guild_id, name, datetime.utcnow().isoformat(), json.dumps(data)),
    )
    conn.commit()
    conn.close()
    return backup_id


def get_backups(guild_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, name, created_at FROM backups WHERE guild_id=? ORDER BY created_at DESC", (guild_id,))
    rows = c.fetchall()
    conn.close()
    return rows


def get_backup(backup_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, guild_id, name, created_at, data FROM backups WHERE id=?", (backup_id,))
    row = c.fetchone()
    conn.close()
    return row


def delete_backup(backup_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM backups WHERE id=?", (backup_id,))
    conn.commit()
    conn.close()


def is_admin():
    async def predicate(interaction: Interaction):
        return await bot.is_owner(interaction.user) or interaction.user.guild_permissions.administrator
    return app_commands.checks.check(predicate)


class ConfirmView(View):
    def __init__(self, backup_id: str, action: str):
        super().__init__(timeout=180)
        self.backup_id = backup_id
        self.action = action

    @discord.ui.button(label="Confirm", style=ButtonStyle.danger)
    async def confirm(self, interaction: Interaction, button: Button):
        if not (interaction.user.guild_permissions.administrator or await bot.is_owner(interaction.user)):
            await interaction.response.send_message("You don't have permission.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            if self.action == "restore":
                await restore_backup(interaction, self.backup_id)
            elif self.action == "delete":
                delete_backup(self.backup_id)
                await interaction.edit_original_response(content=f"Backup `{self.backup_id}` deleted.")
        except Exception as e:
            logger.exception("Error during %s", self.action)
            await interaction.edit_original_response(content=f"Error: {e}")

    @discord.ui.button(label="Cancel", style=ButtonStyle.secondary)
    async def cancel(self, interaction: Interaction, button: Button):
        await interaction.response.edit_message(view=None)


class BackupNameModal(Modal, title="Create Backup"):
    name = TextInput(label="Backup Name", placeholder="Enter a name for this backup")

    def __init__(self):
        super().__init__()

    async def on_submit(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True)
        await create_backup(interaction, self.name.value)


async def create_backup(interaction: Interaction, name: str):
    if not (interaction.user.guild_permissions.administrator or await bot.is_owner(interaction.user)):
        await interaction.followup.send("Only admins can create backups.", ephemeral=True)
        return

    await interaction.followup.send("Creating backup...", ephemeral=True)
    guild = interaction.guild
    data = {
        "name": name,
        "created_at": datetime.utcnow().isoformat(),
        "roles": [],
        "categories": [],
        "channels": [],
        "settings": {
            "name": guild.name,
            "description": guild.description,
            "splash": str(guild.splash) if guild.splash else None,
            "icon": str(guild.icon) if guild.icon else None,
        },
    }

    for role in guild.roles:
        data["roles"].append({
            "id": role.id,
            "name": role.name,
            "color": str(role.color),
            "permissions": str(role.permissions),
            "hoist": role.hoist,
            "display_icon": role.display_icon,
            "mentionable": role.mentionable,
            "position": role.position,
        })

    for category in guild.categories:
        data["categories"].append({
            "id": category.id,
            "name": category.name,
            "position": category.position,
            "permission_overwrites": [
                {
                    "target_type": "role" if isinstance(ow.target, discord.Role) else "member",
                    "target_id": ow.target.id if ow.target else None,
                    "allow": str(ow.allow),
                    "deny": str(ow.deny),
                } for ow in category.overwrites
            ],
        })

    for channel in guild.channels:
        if isinstance(channel, discord.CategoryChannel):
            continue
        data["channels"].append({
            "id": channel.id,
            "name": channel.name,
            "type": str(channel.type),
            "category_id": channel.category_id,
            "position": channel.position,
            "topic": getattr(channel, "topic", None),
            "nsfw": getattr(channel, "nsfw", False),
            "permission_overwrites": [
                {
                    "target_type": "role" if isinstance(ow.target, discord.Role) else "member",
                    "target_id": ow.target.id if ow.target else None,
                    "allow": str(ow.allow),
                    "deny": str(ow.deny),
            } for ow in channel.overwrites
            ],
        })

    backup_id = save_backup(guild.id, name, data)
    embed = Embed(title="Backup Created", color=Colour.green())
    embed.add_field(name="ID", value=backup_id, inline=False)
    embed.add_field(name="Name", value=name, inline=False)
    embed.add_field(name="Created", value=datetime.utcnow().isoformat(), inline=False)
    await interaction.edit_original_response(embed=embed)


async def restore_backup(interaction: Interaction, backup_id: str):
    row = get_backup(backup_id)
    if not row:
        await interaction.edit_original_response(content="Backup not found.")
        return
    _, guild_id, name, created_at, data_json = row
    data = json.loads(data_json)
    guild = interaction.guild

    await interaction.edit_original_response(content="Restoring backup... (this may take a while)")

    # Restore roles
    for role_data in data["roles"]:
        try:
            await guild.create_role(
                name=role_data["name"],
                color=discord.Color(int(role_data["color"].lstrip("#"), 16)) if role_data["color"] else discord.Color.default(),                permissions=discord.Permissions(role_data["permissions"]),
                hoist=role_data["hoist"],
                display_icon=role_data["display_icon"],
                mentionable=role_data["mentionable"],
            )
        except Exception as e:
            logger.warning("Could not create role %s: %s", role_data["name"], e)

    # Restore categories
    category_map = {}
    for cat_data in data["categories"]:
        try:
            category = await guild.create_category(
                name=cat_data["name"],
                position=cat_data["position"],
            )
            category_map[cat_data["id"]] = category.id
        except Exception as e:
            logger.warning("Could not create category %s: %s", cat_data["name"], e)

    # Restore channels
    for ch_data in data["channels"]:
        try:
            overwrites = []
            for ow in ch_data["permission_overwrites"]:
                target = None
                if ow["target_type"] == "role":
                    target = discord.utils.get(guild.roles, id=ow["target_id"])
                else:
                    target = discord.utils.get(guild.members, id=ow["target_id"])
                if target:
                    overwrites.append(discord.PermissionOverwrite.from_pair(
                        discord.Permissions(ow["allow"]), discord.Permissions(ow["deny"])
                    ))
            kwargs = {
                "name": ch_data["name"],
                "position": ch_data["position"],
                "overwrites": overwrites,
            }
            if ch_data["category_id"] in category_map:
                kwargs["category"] = guild.get_channel(category_map[ch_data["category_id"]])
            if ch_data["type"] == "text":
                kwargs["topic"] = ch_data["topic"]
                kwargs["nsfw"] = ch_data["nsfw"]
                await guild.create_text_channel(**kwargs)
            elif ch_data["type"] == "voice":
                await guild.create_voice_channel(**kwargs)
            elif ch_data["type"] == "news":
                await guild.create_text_channel(**kwargs)
        except Exception as e:
            logger.warning("Could not create channel %s: %s", ch_data["name"], e)

    embed = Embed(title="Restore Complete", color=Colour.green())
    embed.add_field(name="Backup", value=f"{name} ({backup_id})")
    embed.add_field(name="Created", value=created_at)
    embed.set_footer(text="Note: Existing channels/roles were not deleted.")
    await interaction.edit_original_response(embed=embed)


class BackupCommands(app_commands.Group):
    def __init__(self):
        super().__init__(name="backup", description="Server backup management")

    @app_commands.command(name="create", description="Create a backup of the server")
    @app_commands.describe(name="Name for the backup")
    @is_admin()
    async def create(self, interaction: Interaction, name: str):
        await create_backup(interaction, name)

    @app_commands.command(name="list", description="List saved backups")
    async def list(self, interaction: Interaction):
        backups = get_backups(interaction.guild_id)
        if not backups:
            await interaction.response.send_message("No backups found.", ephemeral=True)
            return
        embed = Embed(title="Saved Backups", color=Colour.blurple())
        for bid, bname, created in backups:
            embed.add_field(name=bname, value=f"ID: `{bid}`\nCreated: {created}", inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="restore", description="Restore a backup")
    @app_commands.describe(backup="Backup ID")
    @is_admin()
    async def restore(self, interaction: Interaction, backup: str):
        row = get_backup(backup)
        if not row:
            await interaction.response.send_message("Backup not found.", ephemeral=True)
            return
        _, _, name, created_at, _ = row
        embed = Embed(title="Restore Confirmation", color=Colour.orange())
        embed.add_field(name="Backup", value=f"{name} ({backup})")
        embed.add_field(name="Created", value=created_at)
        embed.set_footer(text="Existing channels/roles will NOT be deleted.")
        view = ConfirmView(backup, "restore")
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="delete", description="Delete a backup")
    @app_commands.describe(backup="Backup ID")
    @is_admin()
    async def delete(self, interaction: Interaction, backup: str):
        row = get_backup(backup)
        if not row:
            await interaction.response.send_message("Backup not found.", ephemeral=True)
            return
        _, _, name, created_at, _ = row
        embed = Embed(title="Delete Confirmation", color=Colour.red())
        embed.add_field(name="Backup", value=f"{name} ({backup})")
        embed.add_field(name="Created", value=created_at)
        view = ConfirmView(backup, "delete")
        await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="info", description="Show backup details")
    @app_commands.describe(backup="Backup ID")
    async def info(self, interaction: Interaction, backup: str):
        row = get_backup(backup)
        if not row:
            await interaction.response.send_message("Backup not found.", ephemeral=True)
            return
        _, guild_id, name, created_at, data_json = row
        data = json.loads(data_json)
        embed = Embed(title=f"Backup Info: {name}", color=Colour.blurple())
        embed.add_field(name="ID", value=backup, inline=False)
        embed.add_field(name="Created", value=created_at, inline=False)
        embed.add_field(name="Roles", value=len(data["roles"]), inline=True)
        embed.add_field(name="Categories", value=len(data["categories"]), inline=True)
        embed.add_field(name="Channels", value=len(data["channels"]), inline=True)
        await interaction.response.send_message(embed=embed)


@bot.event
async def on_ready():
    init_db()
    bot.add_app_command(BackupCommands())
    await bot.tree.sync()
    logger.info("Logged in as %s", bot.user)


@bot.tree.command(name="help", description="Show help information")
async def help(interaction: Interaction):
    embed = Embed(title="Backup Bot Help", color=Colour.blurple())
    embed.add_field(name="/backup create", value="Create a new server backup", inline=False)
    embed.add_field(name="/backup list", value="List all saved backups", inline=False)
    embed.add_field(name="/backup restore", value="Restore a backup by ID", inline=False)
    embed.add_field(name="/backup delete", value="Delete a backup by ID", inline=False)
    embed.add_field(name="/backup info", value="Show details of a backup", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.event
async def on_command_error(ctx, error):
    logger.exception("Command error: %s", error)


if __name__ == "__main__":
    bot.run(TOKEN)
