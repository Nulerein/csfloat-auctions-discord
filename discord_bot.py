"""
CSFloat AuctionRadar Discord bot.

Commands:
  /auctions  auctions ending soon, sorted by discount
  /deals     buy-now listings sorted by discount or recency

When ALERT_CHANNEL_ID is set, the bot also checks for matching auctions
every few minutes and posts new finds to that channel.

Install:  pip install requests discord.py python-dotenv
Configure: copy .env.example to .env and fill it in (never share the .env file)
Run:      python discord_bot.py
"""
import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
import requests
from discord import app_commands
from discord.ext import tasks

logger = logging.getLogger(__name__)

try:  # Load .env when python-dotenv is installed.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

API = "https://csfloat.com/api/v1/listings"
ITEM_URL = "https://csfloat.com/item/{}"

TOKEN = os.getenv("DISCORD_TOKEN")
CSFLOAT_KEY = os.getenv("CSFLOAT_API_KEY")
SEEN_AUCTIONS_FILE = Path(__file__).with_name("seen_auctions.json")


def parse_float_setting(
    name,
    raw_value,
    default,
    *,
    minimum=None,
    maximum=None,
    minimum_exclusive=False,
):
    """Parse and validate a finite floating-point environment setting."""
    if raw_value is None or not raw_value.strip():
        return default
    try:
        value = float(raw_value)
    except ValueError:
        raise ValueError(f"{name} must be a number.") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if minimum is not None and (value <= minimum if minimum_exclusive else value < minimum):
        operator = "greater than" if minimum_exclusive else "at least"
        raise ValueError(f"{name} must be {operator} {minimum:g}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum:g}.")
    return value


def parse_optional_discord_id(name, value):
    """Return an optional Discord snowflake string, rejecting non-numeric IDs."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"{name} must contain only digits.")
    return value


MIN_ALERT_INTERVAL_MIN = 1  # Never poll CSFloat more often than once a minute.

try:
    GUILD_ID = parse_optional_discord_id("GUILD_ID", os.getenv("GUILD_ID"))
    ALERT_CHANNEL_ID = parse_optional_discord_id(
        "ALERT_CHANNEL_ID", os.getenv("ALERT_CHANNEL_ID")
    )
    ALERT_INTERVAL_MIN = parse_float_setting(
        "ALERT_INTERVAL_MIN", os.getenv("ALERT_INTERVAL_MIN"), 5,
        minimum=MIN_ALERT_INTERVAL_MIN,
    )
    ALERT_MAX_PRICE = parse_float_setting(
        "ALERT_MAX_PRICE", os.getenv("ALERT_MAX_PRICE"), 30,
        minimum=1, maximum=5000,
    )
    ALERT_HOURS = parse_float_setting(
        "ALERT_HOURS", os.getenv("ALERT_HOURS"), 6,
        minimum=0.1, maximum=168,
    )
    ALERT_MIN_DISCOUNT = parse_float_setting(
        "ALERT_MIN_DISCOUNT", os.getenv("ALERT_MIN_DISCOUNT"), 15
    )
except ValueError as e:
    raise SystemExit(f"Invalid .env configuration: {e}") from None


# ---------- CSFloat API ----------

class CSFloatError(Exception):
    """User-facing error raised when a CSFloat request fails."""


def parse_time(s):
    """Convert an API ISO timestamp to UTC, or return None if it is invalid."""
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    m = re.match(r"(.*?\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
    if m:  # Normalize fractional seconds for compatibility across Python versions.
        head, frac, tail = m.groups()
        s = head + (frac or ".0")[:7].ljust(7, "0") + tail
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load_seen(path=SEEN_AUCTIONS_FILE, now=None):
    """Load valid, unexpired notified auction IDs from the local JSON state file."""
    path = Path(path)
    now = now or datetime.now(timezone.utc)
    try:
        with path.open(encoding="utf-8") as state_file:
            payload = json.load(state_file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"Could not read notification state {path}: {e}") from e

    if not isinstance(payload, dict):
        raise TypeError(f"Invalid notification state format in {path}: expected a JSON object.")

    seen = {}
    for listing_id, expiration in payload.items():
        exp = parse_time(expiration) if isinstance(expiration, str) else None
        if exp is None:
            logger.warning(
                "Skipping invalid notification state for auction ID %r in %s: invalid expiration",
                listing_id,
                path,
            )
            continue
        if exp > now:
            seen[listing_id] = exp
    return seen


def save_seen(seen, path=SEEN_AUCTIONS_FILE):
    """Atomically save notified auction IDs and their expiration times."""
    path = Path(path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as state_file:
            temp_path = Path(state_file.name)
            json.dump(
                {listing_id: expiration.isoformat() for listing_id, expiration in seen.items()},
                state_file,
                ensure_ascii=False,
                indent=2,
            )
            state_file.write("\n")
        os.replace(temp_path, path)
    except (OSError, TypeError, ValueError) as e:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Could not save notification state {path}: {e}") from e


def make_session():
    session = requests.Session()
    session.headers["User-Agent"] = "csfloat-auctionradar-discord/1.0"
    if CSFLOAT_KEY:
        session.headers["Authorization"] = CSFLOAT_KEY
    return session


def api_get(session, params, retries=3):
    for _ in range(retries):
        r = session.get(API, params=params, timeout=20)
        if r.status_code == 429:
            try:
                wait = int(r.headers.get("Retry-After", 10))
            except ValueError:
                wait = 10
            time.sleep(min(wait, 60))
            continue
        if r.status_code in (401, 403):
            raise CSFloatError(f"CSFloat returned {r.status_code}: check CSFLOAT_API_KEY.")
        r.raise_for_status()
        return r.json()
    raise CSFloatError("CSFloat rate limit reached (429). Please try again shortly.")


def unpack(payload):
    """The API returns either a list or {"data": [...], "cursor": "..."}."""
    if isinstance(payload, list):
        return payload, None
    return (payload.get("data") or payload.get("listings") or []), payload.get("cursor")


def ref_price(lot):
    """Return the reference price in cents and its source: CSFloat or Steam fallback."""
    ref = lot.get("reference") or {}
    for key in ("predicted_price", "base_price"):
        if ref.get(key):
            return ref[key], "ref"
    scm = (lot.get("item") or {}).get("scm") or {}
    if scm.get("price"):
        return scm["price"], "steam"
    return None, None


def find_auctions(max_price, hours, pages=4):
    """Find auctions ending within `hours`, sorted by largest discount first.

    This blocking function should be called from the bot via run_in_executor.
    """
    session = make_session()
    params = {
        "type": "auction",
        "sort_by": "expires_soon",
        "max_price": round(max_price * 100),  # The API expects cents.
        "limit": 50,
    }
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=hours)
    rows, cursor = [], None
    for _ in range(pages):
        if cursor:
            params["cursor"] = cursor
        batch, cursor = unpack(api_get(session, params))
        if not batch:
            break
        past_window = False
        for lot in batch:
            if lot.get("state", "listed") != "listed":
                continue
            details = lot.get("auction_details") or {}
            exp = parse_time(details.get("expires_at"))
            if exp is None or exp <= now:
                continue
            if exp > deadline:
                past_window = True  # Results are ordered by expiration; later pages are outside the window.
                break
            ref, src = ref_price(lot)
            if not ref:
                continue
            bid = details.get("min_next_bid") or lot.get("price") or 0  # The current minimum bid.
            item = lot.get("item") or {}
            rows.append({
                "kind": "auction", "id": lot.get("id"), "exp": exp, "bid": bid, "ref": ref,
                "steam": src != "ref", "disc": (ref - bid) / ref * 100,
                "float": item.get("float_value"), "name": item.get("market_hash_name", "?"),
            })
        if past_window or not cursor:
            break
    rows.sort(key=lambda r: r["disc"], reverse=True)
    return rows


def find_deals(max_price, min_price, sort_by, min_sales, pages):
    """Find buy-now listings and sort by reference-price discount.

    sort_by: "highest_discount" (best according to CSFloat) or "most_recent".
    min_sales: minimum sales used to build the reference (0 disables this filter).
    This blocking function should be called from the bot via run_in_executor.
    """
    session = make_session()
    params = {
        "type": "buy_now",
        "sort_by": sort_by,
        "max_price": round(max_price * 100),  # The API expects cents.
        "limit": 50,
    }
    if min_price > 0:
        params["min_price"] = round(min_price * 100)
    if min_sales > 0:
        params["min_ref_qty"] = min_sales
    rows, cursor = [], None
    for _ in range(pages):
        if cursor:
            params["cursor"] = cursor
        batch, cursor = unpack(api_get(session, params))
        if not batch:
            break
        for lot in batch:
            if lot.get("state", "listed") != "listed":
                continue
            ref, src = ref_price(lot)
            price = lot.get("price") or 0
            if not ref or not price:
                continue
            item = lot.get("item") or {}
            rows.append({
                "kind": "deal", "id": lot.get("id"), "bid": price, "ref": ref,
                "steam": src != "ref", "disc": (ref - price) / ref * 100,
                "float": item.get("float_value"), "name": item.get("market_hash_name", "?"),
                "offer": lot.get("min_offer_price"), "created": parse_time(lot.get("created_at")),
            })
        if not cursor:
            break
    rows.sort(key=lambda r: r["disc"], reverse=True)
    return rows


# ---------- Message formatting ----------

def fmt_left(exp):
    secs = max(int((exp - datetime.now(timezone.utc)).total_seconds()), 0)
    h, m = divmod(secs // 60, 60)
    return f"{h}h {m:02d}m"


def fmt_age(created):
    secs = max(int((datetime.now(timezone.utc) - created).total_seconds()), 0)
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def fmt_row(r):
    star = "*" if r["steam"] else ""
    fv = f"{r['float']:.4f}" if r["float"] is not None else "-"
    name = r["name"].replace("[", "(").replace("]", ")")  # Square brackets would break the Markdown link.
    head = f"**{r['disc']:+.1f}%** [{name}]({ITEM_URL.format(r['id'])})"
    price, ref = r["bid"] / 100, r["ref"] / 100
    if r["kind"] == "auction":
        return f"{head}\nbid ${price:.2f} · ref ${ref:.2f}{star} · {fmt_left(r['exp'])} · float {fv}"
    info = f"price ${price:.2f} · ref ${ref:.2f}{star} · float {fv}"
    if r.get("offer") and r["offer"] < r["bid"]:
        info += f" · offers from ${r['offer'] / 100:.2f}"
    if r.get("created"):
        info += f" · {fmt_age(r['created'])}"
    return f"{head}\n{info}"


def build_embed(rows, title):
    lines, total = [], 0
    for r in rows:
        line = fmt_row(r)
        if total + len(line) > 3800:  # The embed description limit is 4096 characters.
            break
        lines.append(line)
        total += len(line) + 2
    embed = discord.Embed(title=title, description="\n\n".join(lines), color=0x2B7FFF)
    if any(r["steam"] for r in rows[: len(lines)]):
        embed.set_footer(text="* No CSFloat reference; comparison uses Steam price (discount may be overstated).")
    return embed


# ---------- Discord bot ----------

class RadarBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.seen = {}  # Listing ID -> expiration time for auctions already posted to the channel.

    async def setup_hook(self):
        if GUILD_ID and GUILD_ID.isdigit():  # Commands appear immediately on a specific server.
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        if ALERT_CHANNEL_ID and ALERT_CHANNEL_ID.isdigit():
            try:
                self.seen = load_seen()
            except (RuntimeError, TypeError, ValueError) as e:
                logger.warning(
                    "Could not load notification state; starting with an empty state: %s",
                    e,
                )
                self.seen = {}
            alert_loop.change_interval(minutes=ALERT_INTERVAL_MIN)
            alert_loop.start()

    async def on_ready(self):
        logger.info("Bot is online as %s. Commands: /auctions, /deals", self.user)


client = RadarBot()


@client.tree.command(name="auctions", description="CSFloat auctions ending soon, best discounts first")
@app_commands.describe(
    max_price="Maximum price in USD",
    hours="Ending within the next N hours",
    top="Number of listings to show",
    min_discount="Minimum discount in percent (negative values are allowed)",
)
async def auctions(
    interaction: discord.Interaction,
    max_price: app_commands.Range[float, 1.0, 5000.0] = 30.0,
    hours: app_commands.Range[float, 0.1, 720.0] = 12.0,
    top: app_commands.Range[int, 1, 30] = 30,
    min_discount: float = 0.0,
):
    await interaction.response.defer(thinking=True)  # The CSFloat request may take a few seconds.
    try:
        rows = await asyncio.get_running_loop().run_in_executor(None, find_auctions, max_price, hours)
    except (CSFloatError, requests.RequestException) as e:
        logger.warning("CSFloat request failed for /auctions: %s", e)
        await interaction.followup.send(f"Could not retrieve CSFloat data: {e}")
        return
    rows = [r for r in rows if r["disc"] >= min_discount][:top]
    if not rows:
        await interaction.followup.send(
            "No listings found. Try increasing max_price or hours, or lowering min_discount.")
        return
    await interaction.followup.send(
        embed=build_embed(rows, f"Auction listings up to ${max_price:g}, ending within {hours:g}h"))


@client.tree.command(name="deals", description="CSFloat buy-now listings with good discounts")
@app_commands.describe(
    max_price="Maximum price in USD",
    min_price="Minimum price in USD (filters out very cheap listings)",
    min_discount="Minimum discount from the reference price, in percent",
    top="Number of listings to show",
    sort="Browse listings with the best discounts or the newest listings",
    min_sales="Minimum sales used for the reference price (0 disables this filter)",
)
@app_commands.choices(sort=[
    app_commands.Choice(name="Highest discount", value="highest_discount"),
    app_commands.Choice(name="Newest", value="most_recent"),
])
async def deals(
    interaction: discord.Interaction,
    max_price: app_commands.Range[float, 1.0, 5000.0] = 30.0,
    min_price: app_commands.Range[float, 0.0, 5000.0] = 1.0,
    min_discount: float = 10.0,
    top: app_commands.Range[int, 1, 30] = 30,
    sort: app_commands.Choice[str] | None = None,
    min_sales: app_commands.Range[int, 0, 1000] = 20,
):
    await interaction.response.defer(thinking=True)
    sort_by = sort.value if sort else "highest_discount"
    pages = 2 if sort_by == "highest_discount" else 4  # Search more pages when looking for recent listings.
    try:
        rows = await asyncio.get_running_loop().run_in_executor(
            None, find_deals, max_price, min_price, sort_by, min_sales, pages)
    except (CSFloatError, requests.RequestException) as e:
        logger.warning("CSFloat request failed for /deals: %s", e)
        await interaction.followup.send(f"Could not retrieve CSFloat data: {e}")
        return
    rows = [r for r in rows if r["disc"] >= min_discount][:top]
    if not rows:
        await interaction.followup.send(
            "No listings found. Try increasing max_price or lowering min_discount or min_sales.")
        return
    mode = "highest discounts" if sort_by == "highest_discount" else "newest listings"
    await interaction.followup.send(
        embed=build_embed(rows, f"Buy-now listings ${min_price:g}-${max_price:g}, {mode}"))


@tasks.loop(minutes=5)  # The actual interval is configured by ALERT_INTERVAL_MIN.
async def alert_loop():
    try:
        channel_id = int(ALERT_CHANNEL_ID)
        channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
        rows = await asyncio.get_running_loop().run_in_executor(None, find_auctions, ALERT_MAX_PRICE, ALERT_HOURS)
        now = datetime.now(timezone.utc)
        client.seen = {i: e for i, e in client.seen.items() if e > now}  # Drop expired auctions.
        new = [r for r in rows if r["disc"] >= ALERT_MIN_DISCOUNT and r["id"] not in client.seen][:10]
        if new:
            await channel.send(embed=build_embed(new, f"New auction deals: {len(new)}"))
            client.seen.update({r["id"]: r["exp"] for r in new})
        save_seen(client.seen)
    except Exception:  # Keep the loop alive if one iteration fails.
        logger.exception("[alerts] error")


@alert_loop.before_loop
async def _wait_until_ready():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Copy .env.example to .env and enter your bot token.")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    client.run(TOKEN, log_handler=None)
