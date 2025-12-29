import discord
from discord import app_commands
from discord.ext import commands
import datetime
import aiosqlite
import os
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable not set")

MODMAIL_CHANNEL_ID = 1455174548495536280  # Replace with your mod mail channel ID
WARNING_ROLE_ID = 1449102427386019991   # @Warned User role ID
RED_COLOR = 0xff0000

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------- DATABASE ----------------

async def init_db():
    async with aiosqlite.connect("mod.db") as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            user_id INTEGER,
            guild_id INTEGER,
            reason TEXT,
            time TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY
        )
        """)
        await db.commit()

async def add_warning(user_id, guild_id, reason):
    async with aiosqlite.connect("mod.db") as db:
        await db.execute(
            "INSERT INTO warnings VALUES (?, ?, ?, ?)",
            (user_id, guild_id, reason, str(datetime.datetime.utcnow()))
        )
        await db.commit()

async def get_warnings(user_id, guild_id):
    async with aiosqlite.connect("mod.db") as db:
        cur = await db.execute(
            "SELECT reason FROM warnings WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        )
        return await cur.fetchall()

async def clear_warnings(user_id, guild_id):
    async with aiosqlite.connect("mod.db") as db:
        await db.execute(
            "DELETE FROM warnings WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        )
        await db.commit()

async def block_user(user_id):
    async with aiosqlite.connect("mod.db") as db:
        await db.execute("INSERT OR IGNORE INTO blocked_users VALUES (?)", (user_id,))
        await db.commit()

async def unblock_user(user_id):
    async with aiosqlite.connect("mod.db") as db:
        await db.execute("DELETE FROM blocked_users WHERE user_id=?", (user_id,))
        await db.commit()

async def is_blocked(user_id):
    async with aiosqlite.connect("mod.db") as db:
        cur = await db.execute("SELECT user_id FROM blocked_users WHERE user_id=?", (user_id,))
        return await cur.fetchone() is not None

# ---------------- EMBED HELPER ----------------

def make_embed(title: str, description: str = None, fields: list = None):
    embed = discord.Embed(title=title, description=description, color=RED_COLOR)
    embed.timestamp = datetime.datetime.utcnow()
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="Moderation Bot")
    return embed

# ---------------- EVENTS ----------------

@bot.event
async def on_ready():
    await init_db()
    await tree.sync()
    print(f"Bot online: {bot.user}")

# ---------------- MOD MAIL ----------------

modmail_sessions = {}  # user_id -> thread

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    mod_channel = bot.get_channel(MODMAIL_CHANNEL_ID)
    if not mod_channel:
        return

    # ----- DM to Mod Mail -----
    if isinstance(message.channel, discord.DMChannel):
        if await is_blocked(message.author.id):
            return  # Blocked users cannot use mod mail

        # Create thread if it doesn't exist
        thread = modmail_sessions.get(message.author.id)
        if not thread:
            thread = await mod_channel.create_thread(
                name=f"Mod Mail - {message.author.name}",
                type=discord.ChannelType.public_thread,
                reason="New Mod Mail"
            )
            modmail_sessions[message.author.id] = thread
            await message.channel.send("✅ Connected to Mod Mail")

        embed = make_embed(
            "📩 New Mod Mail",
            fields=[
                ("User", f"{message.author} ({message.author.id})", True),
                ("Message", message.content, False)
            ]
        )
        sent = await thread.send(embed=embed)
        await sent.add_reaction("✉️")

    # ----- Thread reply to DM -----
    elif isinstance(message.channel, discord.Thread):
        # Only handle threads in mod channel
        if message.channel.parent.id != MODMAIL_CHANNEL_ID:
            return
        # Only mods can reply
        if not message.author.guild_permissions.manage_messages:
            return
        # Find user ID
        for uid, thread in modmail_sessions.items():
            if thread.id == message.channel.id:
                user = bot.get_user(uid)
                if user:
                    await user.send(f"💬 **Support Agent:** {message.content}")
                break

# ---------------- MOD MAIL COMMANDS ----------------

@tree.command(name="close")
@app_commands.checks.has_permissions(manage_messages=True)
async def close(interaction: discord.Interaction):
    """Close the current Mod Mail thread."""
    if not isinstance(interaction.channel, discord.Thread):
        await interaction.response.send_message("❌ This command can only be used in a Mod Mail thread.", ephemeral=True)
        return

    user_id = None
    for uid, thread in modmail_sessions.items():
        if thread.id == interaction.channel.id:
            user_id = uid
            break

    if not user_id:
        await interaction.response.send_message("❌ This thread is not linked to a Mod Mail session.", ephemeral=True)
        return

    modmail_sessions.pop(user_id, None)
    user = bot.get_user(user_id)
    if user:
        await user.send("✅ Your Mod Mail has been closed by the support team.")

    await interaction.channel.edit(archived=True, locked=True)
    embed = make_embed("✅ Mod Mail Closed", f"Thread `{interaction.channel.name}` has been closed.")
    await interaction.response.send_message(embed=embed)

@tree.command(name="block")
@app_commands.checks.has_permissions(manage_messages=True)
async def block(interaction: discord.Interaction, member: discord.User):
    """Block a user from using Mod Mail."""
    await block_user(member.id)
    thread = modmail_sessions.pop(member.id, None)
    if thread:
        await thread.edit(archived=True, locked=True)
    embed = make_embed("⛔ User Blocked", f"{member} has been blocked from using Mod Mail.")
    await interaction.response.send_message(embed=embed)

@tree.command(name="unblock")
@app_commands.checks.has_permissions(manage_messages=True)
async def unblock(interaction: discord.Interaction, member: discord.User):
    """Unblock a user from using Mod Mail."""
    await unblock_user(member.id)
    embed = make_embed("✅ User Unblocked", f"{member} can now use Mod Mail again.")
    await interaction.response.send_message(embed=embed)

# ---------------- WARNING COMMAND WITH PROOF ----------------

@tree.command(name="warn")
@app_commands.checks.has_permissions(kick_members=True)
@app_commands.describe(
    user="User to warn",
    channel="Channel to post the warning",
    reason="Reason for warning",
    attachment="Attach an image as proof"
)
async def warn(
    interaction: discord.Interaction,
    user: discord.Member,
    channel: discord.TextChannel,
    reason: str,
    attachment: discord.Attachment
):
    if not attachment:
        await interaction.response.send_message("❌ You must attach an image as proof.", ephemeral=True)
        return

    # Assign the warning role
    role = interaction.guild.get_role(WARNING_ROLE_ID)
    if role:
        try:
            await user.add_roles(role, reason=f"Warned by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I cannot assign the warning role. Check role hierarchy and permissions.", ephemeral=True
            )
            return

    # Add to database
    await add_warning(user.id, interaction.guild.id, reason)
    warns = await get_warnings(user.id, interaction.guild.id)
    warning_number = len(warns)

    # Send embed to log channel
    embed = discord.Embed(
        title=f"⚠️ Warning #{warning_number}",
        description=f"**User:** {user.mention}\n**Reason:** {reason}",
        color=RED_COLOR
    )
    embed.set_image(url=attachment.url)
    embed.timestamp = datetime.datetime.utcnow()
    embed.set_footer(text=f"Issued by {interaction.user}", icon_url=interaction.user.avatar.url if interaction.user.avatar else None)

    await channel.send(embed=embed)
    await interaction.response.send_message(f"✅ {user.mention} has been warned and logged in {channel.mention}", ephemeral=True)

# ---------------- CLEAR WARNINGS COMMAND ----------------

@tree.command(name="clearwarnings")
@app_commands.checks.has_permissions(administrator=True)
async def clearwarnings(interaction: discord.Interaction, member: discord.Member):
    """
    Clear all warnings for a user and remove the warning role.
    """
    await clear_warnings(member.id, interaction.guild.id)

    # Remove warning role
    role = interaction.guild.get_role(WARNING_ROLE_ID)
    if role:
        try:
            await member.remove_roles(role, reason=f"Warnings cleared by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I cannot remove the warning role. Check role hierarchy and permissions.", ephemeral=True
            )
            return

    embed = make_embed(
        "✅ Warnings Cleared",
        f"All warnings for {member} have been cleared and {role.name} role removed."
    )
    await interaction.response.send_message(embed=embed)

# ---------------- OTHER MODERATION COMMANDS ----------------

@tree.command(name="warnings")
async def warnings(interaction: discord.Interaction, member: discord.Member):
    warns = await get_warnings(member.id, interaction.guild.id)
    description = "\n".join(f"{i+1}. {w[0]}" for i, w in enumerate(warns)) if warns else "No warnings"
    embed = make_embed(f"Warnings for {member}", description)
    await interaction.response.send_message(embed=embed)

@tree.command(name="kick")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    await member.kick(reason=reason)
    embed = make_embed("👢 User Kicked", f"Member: {member}\nReason: {reason}")
    await interaction.response.send_message(embed=embed)

@tree.command(name="ban")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason"):
    await member.ban(reason=reason)
    embed = make_embed("⛔ User Banned", f"Member: {member}\nReason: {reason}")
    await interaction.response.send_message(embed=embed)

@tree.command(name="timeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout(interaction: discord.Interaction, member: discord.Member, minutes: int):
    await member.timeout(datetime.timedelta(minutes=minutes))
    embed = make_embed("🕒 User Timed Out", f"Member: {member}\nDuration: {minutes} minutes")
    await interaction.response.send_message(embed=embed)

@tree.command(name="untimeout")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, member: discord.Member):
    await member.timeout(None)
    embed = make_embed("✅ Timeout Removed", f"Timeout removed for {member}")
    await interaction.response.send_message(embed=embed)

@tree.command(name="purge")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, amount: int):
    await interaction.channel.purge(limit=amount)
    embed = make_embed("🧹 Messages Purged", f"{amount} messages deleted")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="lock")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=False)
    embed = make_embed("🔒 Channel Locked", f"{interaction.channel.mention} is now locked")
    await interaction.response.send_message(embed=embed)

@tree.command(name="unlock")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    await interaction.channel.set_permissions(interaction.guild.default_role, send_messages=True)
    embed = make_embed("🔓 Channel Unlocked", f"{interaction.channel.mention} is now unlocked")
    await interaction.response.send_message(embed=embed)

# ---------------- KEEP-ALIVE HTTP SERVER ----------------
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'Bot is alive!')

def run_server():
    server = HTTPServer(('', 3000), Handler)  # You can change 3000 to another port if needed
    server.serve_forever()

# Start HTTP server in a separate thread so your bot can still run
threading.Thread(target=run_server, daemon=True).start()


# ---------------- RUN ----------------

bot.run(TOKEN)