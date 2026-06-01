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

load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Optional:
# If you want the bot to post "Show me" buttons only in certain channels,
# add ENABLED_CHANNEL_IDS in Render or .env as a comma-separated list:
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


def already_posted_today(message):
	state = load_state()
	key = get_thread_or_channel_key(message)
	return state.get(key) == today_key()


def mark_posted_today(message):
	state = load_state()
	key = get_thread_or_channel_key(message)
	state[key] = today_key()
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

# Match standalone 4-letter uppercase codes, not pieces of longer words.
CODE_PATTERN = re.compile(r"\b[A-Z]{4}\b")


def decode_codes_in_text(text):
	found = []

	for match in CODE_PATTERN.finditer(text or ""):
		code = match.group(0).upper()
		common_name = BIRD_CODES.get(code)

		if common_name:
			found.append((code, common_name))

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
	If the message is inside a thread or forum post, message.channel.name is
	the thread/forum post title. For ordinary text channels, this function
	returns an empty string so the bot does not scan channel names.
	"""
	if isinstance(message.channel, discord.Thread):
		return message.channel.name or ""

	return ""


def decode_codes_from_message_and_title(message):
	message_results = decode_codes_in_text(message.content or "")
	title_results = decode_codes_in_text(get_thread_title_text_from_message(message))

	return unique_results(message_results + title_results)


def format_results(results):
	if not results:
		return "I didn’t find any recognized bird codes in that message or thread title."

	return "\n".join(f"**{code}** = {common_name}" for code, common_name in results)


class ShowBirdCodesView(discord.ui.View):
	def __init__(self):
		# timeout=None makes the button view persistent while the bot is running.
		super().__init__(timeout=None)

	@discord.ui.button(
		label="Show me",
		style=discord.ButtonStyle.secondary,
		emoji="🐦",
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

		results = decode_codes_from_message_and_title(source_message)
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
	# Ignore bot messages, including this bot's own helper messages.
	if message.author.bot:
		return

	# Optional channel restriction.
	# If ENABLED_CHANNEL_IDS is empty, the bot works in all channels it can read.
	if not channel_is_enabled(message):
		return

	results = decode_codes_from_message_and_title(message)

	if not results:
		return

	# Limit automatic helper reply to once per UTC day per thread/channel.
	if already_posted_today(message):
		return

	try:
		await message.reply(
			"Bird codes detected.",
			view=ShowBirdCodesView(),
			mention_author=False,
			allowed_mentions=discord.AllowedMentions.none(),
		)
		mark_posted_today(message)
	except discord.Forbidden:
		# Bot lacks permission to reply in this channel.
		pass
	except discord.HTTPException:
		# Avoid crashing the bot for one failed helper message.
		pass


@bot.tree.command(name="birdcode", description="Look up a 4-letter bird code.")
@app_commands.describe(code="Example: AMRO, NOCA, BCCH")
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
			f"I don’t recognize **{normalized}** as a bird code in the current list.",
			ephemeral=True,
		)


if not DISCORD_TOKEN:
	raise RuntimeError("Missing DISCORD_TOKEN environment variable.")

bot.run(DISCORD_TOKEN)