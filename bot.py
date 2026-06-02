import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
CODES_FILE = BASE_DIR / "bird_codes.csv"
STATE_FILE = BASE_DIR / "bird_code_state.json"

DEFAULT_HELPER_MESSAGE_TEXT = "🐦 Bird code detected"
NFC_HELPER_MESSAGE_TEXT = "🌙 +🎙️ NFC code detected"

PRIOR_MESSAGE_LOOKBACK_LIMIT = 50

NFC_CODES = {
    "CUPS",
    "SWLI",
    "SFHS",
    "HSSP",
    "SBUF",
    "DESP",
    "DEWA",
    "BUNT",
    "GROS",
    "THSH",
    "GCBI",
    "ZEEP",
    "DBUP",
    "BZWA",
    "CCBRS",
    "MWAR",
    "TANA",
    "BLUEB",
}

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Optional:
# If you want the bot to post "Show me" buttons only in certain channels,
# add ENABLED_CHANNEL_IDS in Northflank or .env as a comma-separated list:
# ENABLED_CHANNEL_IDS=123456789,987654321
#
# For forum posts/threads, this code checks both:
# - the thread's own channel ID
# - the parent forum/channel ID
#
# So you can list either the thread/forum parent channel ID or a specific thread ID.
ENABLED_CHANNEL_IDS_RAW = os.getenv("ENABLED_CHANNEL_IDS", "").strip()

if ENABLED_CHANNEL_IDS_RAW:
    ENABLED_CHANNEL_IDS = {
        int(channel_id.strip())
        for channel_id in ENABLED_CHANNEL_IDS_RAW.split(",")
        if channel_id.strip()
    }
else:
    ENABLED_CHANNEL_IDS = set()


def today_key():
    """Use UTC date for the once-per-day cooldown."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    try:
        with STATE_FILE.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
    except OSError:
        # Do not crash the bot if state cannot be saved.
        pass


def get_thread_or_channel_key(message):
    """
    Discord threads are channels.

    For a thread/forum post, message.channel.id is the thread ID.
    For a normal text channel, message.channel.id is the channel ID.
    """
    guild_id = message.guild.id if message.guild else "dm"
    channel_id = message.channel.id
    return f"{guild_id}:{channel_id}"


def get_codes_from_results(results):
    """
    Return unique codes from a decoded result list, preserving order.
    """
    codes = []
    seen = set()

    for code, _common_name in results:
        if code not in seen:
            codes.append(code)
            seen.add(code)

    return codes


def get_new_codes_today(message, results):
    """
    Return codes in this message/title that have not yet triggered the helper
    today in this thread/channel.
    """
    state = load_state()
    key = get_thread_or_channel_key(message)
    today = today_key()

    all_codes_in_message = get_codes_from_results(results)

    thread_state = state.get(key, {})

    # Backward compatibility: older state used state[key] = "YYYY-MM-DD".
    # If we see that old format, ignore it and start using per-code tracking.
    if not isinstance(thread_state, dict):
        thread_state = {}

    seen_today = set(thread_state.get(today, []))

    return [
        code
        for code in all_codes_in_message
        if code not in seen_today
    ]


def mark_codes_seen_today(message, codes):
    """
    Mark specific codes as having triggered the helper today in this thread/channel.
    """
    if not codes:
        return

    state = load_state()
    key = get_thread_or_channel_key(message)
    today = today_key()

    thread_state = state.get(key, {})

    # Backward compatibility with old state format.
    if not isinstance(thread_state, dict):
        thread_state = {}

    seen_today = set(thread_state.get(today, []))
    seen_today.update(codes)

    # Keep only today's data for this thread/channel so the file does not grow forever.
    state[key] = {
        today: sorted(seen_today)
    }

    save_state(state)


def channel_is_enabled(message):
    """
    If no channel filter is set, allow all channels the bot can read.

    If a channel filter is set:
    - allow a normal channel if its ID is listed
    - allow a thread if either the thread ID or its parent channel ID is listed
    """
    if not ENABLED_CHANNEL_IDS:
        return True

    channel = message.channel

    if channel.id in ENABLED_CHANNEL_IDS:
        return True

    parent = getattr(channel, "parent", None)
    if parent and parent.id in ENABLED_CHANNEL_IDS:
        return True

    return False


def load_bird_codes():
    codes = {}

    with CODES_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            code = row["code"].strip().upper()
            common_name = row["common_name"].strip()

            if code and common_name:
                codes[code] = common_name

    return codes


BIRD_CODES = load_bird_codes()

# Match standalone uppercase 4- or 5-letter codes, plus one special lowercase
# NFC call-type exception: "zeep".
#
# This means:
# - AMRO works
# - CUPS works
# - CCBRS works
# - BLUEB works
# - ZEEP works
# - zeep also works and outputs as ZEEP
# - cups, amro, ccbrs, blueb do not auto-trigger
CODE_PATTERN = re.compile(r"\b(?:[A-Z]{4,5}|zeep)\b")


def normalize_search_text(text):
    """
    Normalize text for common-name matching.

    This makes matching more forgiving around case, punctuation, apostrophes,
    slashes, and repeated spaces.
    """
    if not text:
        return ""

    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def text_contains_common_name(text, common_name):
    """
    Return True if the common name appears in the supplied text.

    This is case-insensitive and punctuation-tolerant.
    """
    normalized_text = normalize_search_text(text)
    normalized_name = normalize_search_text(common_name)

    if not normalized_text or not normalized_name:
        return False

    return normalized_name in normalized_text


async def common_name_appears_in_prior_messages(message, common_name):
    """
    Check whether the common name appeared in earlier messages in the same
    channel/thread.

    The limit is intentionally capped at PRIOR_MESSAGE_LOOKBACK_LIMIT so the bot
    does not scan a very long channel or forum thread every time someone posts
    a code.
    """
    try:
        async for prior_message in message.channel.history(
            limit=PRIOR_MESSAGE_LOOKBACK_LIMIT,
            before=message,
            oldest_first=False,
        ):
            if prior_message.author.bot:
                continue

            if text_contains_common_name(prior_message.content or "", common_name):
                return True

    except (discord.Forbidden, discord.HTTPException):
        # If history lookup fails, do not suppress the bot.
        return False

    return False


def decode_single_raw_code(raw_code):
    """
    Normalize one matched raw code and look it up.
    """
    if raw_code == "zeep":
        code = "ZEEP"
    else:
        code = raw_code.upper()

    common_name = BIRD_CODES.get(code)

    if not common_name:
        return None

    return code, common_name


def decode_codes_in_text(text):
    """
    Decode all recognized codes in a text string without applying the
    common-name-before-code suppression rule.
    """
    found = []

    for match in CODE_PATTERN.finditer(text or ""):
        decoded = decode_single_raw_code(match.group(0))

        if decoded:
            found.append(decoded)

    return found


def unique_results(results):
    unique = []
    seen = set()

    for code, common_name in results:
        if code not in seen:
            unique.append((code, common_name))
            seen.add(code)

    return unique


def get_thread_title_text_from_message(message):
    """
    If the message is inside a thread or forum post, message.channel.name is the
    thread/forum post title.

    For ordinary text channels, this function returns an empty string so the bot
    does not scan channel names.
    """
    if isinstance(message.channel, discord.Thread):
        return message.channel.name or ""

    return ""


async def decode_codes_from_message_and_title(message):
    """
    Decode codes from the current message and thread/forum title.

    Suppress a matched code if that code's common name already appears:
    - in the thread/forum title
    - earlier in the same message before the code
    - in one of the previous PRIOR_MESSAGE_LOOKBACK_LIMIT messages in the same
      channel/thread
    """
    results = []

    message_text = message.content or ""
    title_text = get_thread_title_text_from_message(message)

    # Decode codes in the thread/forum title.
    # If the title itself contains both the common name and the code, suppress.
    for match in CODE_PATTERN.finditer(title_text):
        decoded = decode_single_raw_code(match.group(0))

        if not decoded:
            continue

        code, common_name = decoded

        if text_contains_common_name(title_text, common_name):
            continue

        results.append((code, common_name))

    # Decode codes in the message, applying the common-name-before-code rule.
    for match in CODE_PATTERN.finditer(message_text):
        decoded = decode_single_raw_code(match.group(0))

        if not decoded:
            continue

        code, common_name = decoded
        text_before_code = message_text[:match.start()]

        if text_contains_common_name(title_text, common_name):
            continue

        if text_contains_common_name(text_before_code, common_name):
            continue

        if await common_name_appears_in_prior_messages(message, common_name):
            continue

        results.append((code, common_name))

    return unique_results(results)


def get_helper_message_text(results):
    """
    Use the NFC helper text if any matched code is one of the NFC call-type codes.
    Otherwise use the default bird-code helper text.
    """
    for code, _common_name in results:
        if code in NFC_CODES:
            return NFC_HELPER_MESSAGE_TEXT

    return DEFAULT_HELPER_MESSAGE_TEXT


def format_results(results):
    if not results:
        return "I didn’t find any recognized bird codes in that message or thread title."

    return "\n".join(
        f"**{code}** = {common_name}"
        for code, common_name in results
    )


def is_helper_message(message):
    """
    Return True for this bot's own helper/display messages.

    This is extra protection against recursive helper behavior if Discord surfaces
    an interaction/helper message in a way that reaches on_message.
    """
    if not message.author.bot:
        return False

    content = (message.content or "").strip()

    if content in {DEFAULT_HELPER_MESSAGE_TEXT, NFC_HELPER_MESSAGE_TEXT}:
        return True

    # The ephemeral button response usually has one or more decoded lines like:
    # **CUPS** = Cup-shaped sparrow call type.
    if re.search(r"\*\*[A-Z]{4,5}\*\*\s*=", content):
        return True

    return False


class ShowBirdCodesView(discord.ui.View):
    def __init__(self):
        # timeout=None makes the button view persistent while the bot is running.
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Show me",
        style=discord.ButtonStyle.secondary,
        custom_id="bird_code_helper_show_me",
    )
    async def show_me(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        helper_message = interaction.message

        if not helper_message or not helper_message.reference:
            await interaction.response.send_message(
                "I couldn’t find the original message.",
                ephemeral=True,
            )
            return

        source_message_id = helper_message.reference.message_id

        if not source_message_id:
            await interaction.response.send_message(
                "I couldn’t find the original message.",
                ephemeral=True,
            )
            return

        # Guard against an accidental self-reference.
        if source_message_id == helper_message.id:
            await interaction.response.send_message(
                "I couldn’t find the original user message.",
                ephemeral=True,
            )
            return

        try:
            source_message = await interaction.channel.fetch_message(source_message_id)
        except discord.NotFound:
            await interaction.response.send_message(
                "I couldn’t find the original message. It may have been deleted.",
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don’t have permission to read the original message.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "Something went wrong while reading the original message.",
                ephemeral=True,
            )
            return

        # Do not decode bot/helper messages. This prevents a helper response from
        # becoming the source for another helper-style response.
        if source_message.author.bot or is_helper_message(source_message):
            await interaction.response.send_message(
                "I couldn’t find the original user message.",
                ephemeral=True,
            )
            return

        results = await decode_codes_from_message_and_title(source_message)
        response = format_results(results)

        await interaction.response.send_message(
            response,
            ephemeral=True,
        )


intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    # Re-register the persistent button view after startup.
    bot.add_view(ShowBirdCodesView())

    print(f"Logged in as {bot.user}.")

    # Since the Apps/context-menu command has been removed,
    # this should sync only one global command: /birdcode.
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} global command(s).")

    # Optional one-time cleanup for your original test server:
    # If GUILD_ID is present, this clears old server-specific commands,
    # including the former Apps → Bird Code Helper → Decode bird codes command.
    guild_id = os.getenv("GUILD_ID")

    if guild_id:
        guild = discord.Object(id=int(guild_id))
        bot.tree.clear_commands(guild=guild)
        guild_synced = await bot.tree.sync(guild=guild)

        print(f"Cleared server-specific commands for {guild_id}.")
        print(f"Synced {len(guild_synced)} server-specific command(s).")


@bot.event
async def on_message(message):
    # Ignore bot messages, webhook messages, and this bot's own helper messages.
    if message.author.bot:
        return

    if getattr(message, "webhook_id", None):
        return

    if is_helper_message(message):
        return

    # Optional channel restriction.
    # If ENABLED_CHANNEL_IDS is empty, the bot works in all channels it can read.
    if not channel_is_enabled(message):
        return

    results = await decode_codes_from_message_and_title(message)

    if not results:
        return

    # Limit automatic helper reply to once per UTC day per code per thread/channel.
    new_codes_today = get_new_codes_today(message, results)

    if not new_codes_today:
        return

    helper_message_text = get_helper_message_text(results)

    try:
        await message.reply(
            helper_message_text,
            view=ShowBirdCodesView(),
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        mark_codes_seen_today(message, new_codes_today)

    except discord.Forbidden as e:
        print(f"reply failed: discord.Forbidden: {e}")
    except discord.HTTPException as e:
        print(f"reply failed: discord.HTTPException: {e}")


@bot.tree.command(name="birdcode", description="Look up a 4- or 5-letter bird/NFC code.")
@app_commands.describe(code="Example: AMRO, NOCA, CUPS, CCBRS, zeep")
async def birdcode(interaction: discord.Interaction, code: str):
    normalized = code.strip().upper()
    common_name = BIRD_CODES.get(normalized)

    if common_name:
        await interaction.response.send_message(
            f"**{normalized}** = {common_name}",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"I don’t recognize **{normalized}** as a bird/NFC code in the current list.",
            ephemeral=True,
        )


if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

bot.run(DISCORD_TOKEN)
