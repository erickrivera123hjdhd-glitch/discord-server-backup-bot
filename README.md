# Discord Server Backup Bot

A simple Discord server backup bot built with discord.py 2.x.

## Features

- `/backup create` — Save channels, categories, roles, permissions, and basic settings
- `/backup list` — Show saved backups
- `/backup restore <backup>` — Restore a selected backup
- `/backup delete <backup>` — Delete a backup
- `/backup info <backup>` — Show backup details
- `/help` — Show help information
- Unique backup IDs (UUID)
- SQLite storage
- Admin-only create/restore/delete
- Progress messages during operations
- Confirmation buttons before restore/delete
- Clean Discord embeds

## Setup

1. Install Python 3.11+
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a Discord application at [Discord Developer Portal](https://discord.com/developers/applications)
4. Go to the Bot tab and copy the token
5. Create a `.env` file:
   ```bash
   cp .env.example .env
   ```
6. Edit `.env` and paste your bot token
7. Invite the bot with this URL (replace CLIENT_ID):
   ```
   https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID&permissions=8&scope=bot%20applications.commands
   ```

## Running

```bash
python main.py
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Discord bot token (required) |
| `DB_PATH` | Path to SQLite database file (optional, default: `backups.db`) |

## Notes

- Only server administrators can create, restore, or delete backups
- Existing channels and roles are never automatically deleted during restore
- A confirmation button is shown before any destructive action
