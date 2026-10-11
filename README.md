# CSFloat AuctionRadar Discord

<div align="center">

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Discord.py](https://img.shields.io/badge/discord.py-2.x-5865F2.svg)](https://discordpy.readthedocs.io/)

</div>

An unofficial Discord bot for finding CS2 skin deals on CSFloat. It searches auctions and buy-now listings, and can automatically alert a Discord channel about promising auctions.

> **Disclaimer:** CSFloat AuctionRadar Discord is an unofficial community project. It is not affiliated with or endorsed by CSFloat.

## Features

- `/auctions` — auctions ending soon, sorted by discount.
- `/deals` — buy-now listings sorted by discount or recency.
- Optional automatic alerts for matching auctions in a chosen Discord channel.
- Compares listing prices against CSFloat references, falling back to Steam prices when needed. Steam comparisons are marked with `*`.
- Configuration through `.env`; keep your bot token and API key private.

## Example output

```text
**+18.1%** [AK-47 | Neon Revolution (Minimal Wear)](https://csfloat.com/item/12345)
bid $14.90 · ref $18.20 · 2h 15m · float 0.1234

**+12.0%** [USP-S | Royal Blue](https://csfloat.com/item/67890)
price $9.50 · ref $10.80 · float 0.0345 · 18m ago
```

The bot posts matching auctions and buy-now listings directly in Discord.

## Requirements

- Python 3.11 or newer
- A Discord application and bot token
- A CSFloat API key (recommended for reliable requests)

## Installation and setup

### Windows (PowerShell)

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
```

### Configure `.env`

Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications). Add the bot to your server with the OAuth2 scopes `bot` and `applications.commands`, and grant it permission to send messages and embed links. Get the bot token from the **Bot** page. Create a CSFloat API key from the **Developer** tab in your CSFloat profile.

Fill in `.env`:

| Variable | Required | Description |
| --- | --- | --- |
| `DISCORD_TOKEN` | Yes | Discord bot token. |
| `CSFLOAT_API_KEY` | No, but recommended | CSFloat API key. |
| `GUILD_ID` | No | Discord server ID. When set, commands are synced to that server immediately. |
| `ALERT_CHANNEL_ID` | No | Channel ID for automatic alerts. If empty, the bot only responds to commands. |
| `ALERT_INTERVAL_MIN` | No | How often to check auctions, in minutes (at least `1`). Default: `5`. |
| `ALERT_MAX_PRICE` | No | Maximum price for alerts, in USD. Default: `30`. |
| `ALERT_HOURS` | No | Alert for auctions ending within this many hours. Default: `6`. |
| `ALERT_MIN_DISCOUNT` | No | Minimum discount for alerts, in percent. Default: `15`. |

Never publish `.env` or share your bot token or API key.

Previously sent alerts are stored in a local `seen_auctions.json` file next to the script. The file is updated automatically and is excluded from Git.

On startup, the bot validates numeric settings and server/channel IDs. Invalid values stop startup with an error that identifies the setting and expected format.

### Run

```bash
python discord_bot.py
```

Keep the process running so the bot can handle commands and send alerts. Press `Ctrl+C` to stop it.

## Slash commands

### `/auctions`

Set command options directly in Discord.

| Option | Default | Description |
| --- | --- | --- |
| `max_price` | `30` | Maximum price in USD (`1` to `5000`). |
| `hours` | `12` | Show auctions ending within this many hours (`0.1` to `720`, up to 30 days). |
| `top` | `30` | Maximum number of results (`1` to `30`). |
| `min_discount` | `0` | Minimum discount in percent; negative values are allowed. |

### `/deals`

| Option | Default | Description |
| --- | --- | --- |
| `max_price` | `30` | Maximum price in USD (`1` to `5000`). |
| `min_price` | `1` | Minimum price in USD (`0` to `5000`). |
| `min_discount` | `10` | Minimum discount from the reference price, in percent. |
| `top` | `30` | Maximum number of results (`1` to `30`). |
| `sort` | `Highest discount` | Choose between the highest discounts and newest listings. |
| `min_sales` | `20` | Minimum number of sales used for the reference price (`0` to `1000`); `0` disables this filter. |

## License

This project is licensed under the [MIT License](LICENSE).
