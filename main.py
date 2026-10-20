import os
import io
import re
import json
import time
import datetime
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

intents = discord.Intents.default()
intents.members = True
intents.guilds = True
intents.moderation = True
intents.message_content = True

# =====================================================================
# CONFIGURACIÓN
# =====================================================================
DATA_FILE = os.getenv("DATA_FILE", "nexus_data.json")
# IDs extra de dueños del bot (opcional): variable de entorno OWNER_IDS="123,456"
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()}

MENTION_LIMIT = 5            # menciones (usuarios + roles) por mensaje
MENTION_WINDOW_LIMIT = 8     # menciones acumuladas en la ventana
MENTION_WINDOW = 10          # segundos
SPAM_MESSAGES = 5            # mensajes ...
SPAM_WINDOW = 5              # ... en estos segundos
ANTINUKE_LIMIT = 3           # acciones destructivas ...
ANTINUKE_WINDOW = 10         # ... en estos segundos
RAID_JOIN_LIMIT = 5          # entradas ...
RAID_JOIN_WINDOW = 10        # ... en estos segundos
RAID_MODE_SECONDS = 300      # duración del modo raid
RAID_MIN_ACCOUNT_AGE_DAYS = 7
WARN_TIMEOUT_AT = 3          # cada 3 advertencias = 1 hora de timeout

MODULES = {
    "antinuke": "AntiNuke (canales, roles, bans, bots)",
    "antiraid": "AntiRaid (entradas masivas)",
    "antiping": "Anti Ping Masivo",
    "antispam": "AntiSpam",
    "antiinvite": "Anti Invitaciones",
}

# =====================================================================
# DATOS PERSISTENTES (JSON)
# Nota: en Render gratis el disco se borra en cada deploy.
# =====================================================================
DEFAULT_DATA = {"gbans": {}, "whitelist": {}, "warns": {}, "lang": {}, "config": {}, "lockdown": {}}


def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    for key in DEFAULT_DATA:
        data.setdefault(key, {})
    return data


DATA = load_data()


def save_data():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DATA, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except OSError as e:
        print(f"No se pudieron guardar los datos: {e}")


def module_enabled(guild_id, module):
    return DATA["config"].get(str(guild_id), {}).get(module, True)


# =====================================================================
# CONTADORES EN MEMORIA
# =====================================================================
ACTION_COUNTER = {}
MENTION_TRACK = {}
SPAM_TRACK = {}
JOIN_TRACK = {}
RAID_MODE_UNTIL = {}


def register_event(store, key, window, amount=1):
    now = time.time()
    entries = [(t, n) for (t, n) in store.get(key, []) if now - t < window]
    entries.append((now, amount))
    store[key] = entries
    return sum(n for _, n in entries)


def raid_active(guild_id):
    return RAID_MODE_UNTIL.get(guild_id, 0) > time.time()


# =====================================================================
# MENSAJES MULTILINGÜES
# =====================================================================
TICKET_TEXTS = {
    "es": {
        "already_open": "❌ **[ERROR]** Ya tienes un ticket abierto en {channel}",
        "welcome_title": "🎟️ Ticket Privado de Soporte",
        "welcome_desc": "👋 Hola {user}, bienvenido a tu ticket.\n\n🛡️ Un **Administrador** te atenderá en breve. Por favor, describe tu consulta o reporte con detalle.",
        "closing": "⏳ **[TICKET]** Generando transcripción y eliminando canal en 5 segundos...",
        "no_perm_close": "❌ **[ERROR]** Solo los Administradores o el creador del ticket pueden cerrarlo.",
        "created": "✅ **[OK]** Ticket privado creado con éxito en {channel}"
    },
    "en": {
        "already_open": "❌ **[ERROR]** You already have an open ticket in {channel}",
        "welcome_title": "🎟️ Private Support Ticket",
        "welcome_desc": "👋 Hello {user}, welcome to your ticket.\n\n🛡️ An **Administrator** will assist you shortly. Please describe your issue in detail.",
        "closing": "⏳ **[TICKET]** Generating transcript and deleting channel in 5 seconds...",
        "no_perm_close": "❌ **[ERROR]** Only Administrators or the ticket creator can close it.",
        "created": "✅ **[OK]** Private ticket created in {channel}"
    }
}

MSG = {
    "es": {
        "dm_blocked": "🚫 **[ERROR]** Los comandos de Nexus Protect solo se pueden usar dentro de un servidor, no por mensajes directos (MD).",
        "no_perm": "⛔ **[ACCESO DENEGADO]** No tienes los permisos necesarios para ejecutar este comando.",
        "bot_owner_only": "⛔ **[ACCESO DENEGADO]** Solo el dueño del bot puede usar este comando.",
        "guild_owner_only": "⛔ **[ACCESO DENEGADO]** Solo el dueño del servidor puede usar este comando.",
        "hierarchy": "❌ **[ERROR]** No puedes realizar esta acción sobre ese usuario (jerarquía de roles o usuario protegido).",
        "bad_id": "❌ **[ERROR]** ID de usuario inválida.",
        "generic_error": "❌ **[ERROR]** Ocurrió un error inesperado al ejecutar el comando.",
        "bot_no_perm": "❌ **[ERROR]** El bot no tiene permisos suficientes para realizar esta acción.",
        "not_text": "❌ **[ERROR]** Este comando solo funciona en canales de texto.",
        "mention_warn": "🚨 {user}, no se permiten menciones masivas. Has sido silenciado temporalmente.",
        "invite_warn": "🚨 {user}, no se permiten invitaciones a otros servidores.",
        "spam_warn": "🚨 {user}, estás enviando mensajes demasiado rápido. Has sido silenciado temporalmente.",
    },
    "en": {
        "dm_blocked": "🚫 **[ERROR]** Nexus Protect commands can only be used inside a server, not in direct messages (DMs).",
        "no_perm": "⛔ **[ACCESS DENIED]** You don't have the required permissions to run this command.",
        "bot_owner_only": "⛔ **[ACCESS DENIED]** Only the bot owner can use this command.",
        "guild_owner_only": "⛔ **[ACCESS DENIED]** Only the server owner can use this command.",
        "hierarchy": "❌ **[ERROR]** You can't perform this action on that user (role hierarchy or protected user).",
        "bad_id": "❌ **[ERROR]** Invalid user ID.",
        "generic_error": "❌ **[ERROR]** An unexpected error occurred while running the command.",
        "bot_no_perm": "❌ **[ERROR]** The bot doesn't have enough permissions to perform this action.",
        "not_text": "❌ **[ERROR]** This command only works in text channels.",
        "mention_warn": "🚨 {user}, mass mentions are not allowed. You have been temporarily muted.",
        "invite_warn": "🚨 {user}, invites to other servers are not allowed.",
        "spam_warn": "🚨 {user}, you are sending messages too fast. You have been temporarily muted.",
    }
}


# =====================================================================
# IDIOMA DEL USUARIO
# =====================================================================
def _lang_from_roles(roles):
    names = {r.name for r in roles}
    if "English" in names:
        return "en"
    if "Español" in names:
        return "es"
    return None


def get_lang(user, guild=None, locale=None):
    roles = getattr(user, "roles", None)
    if roles:
        found = _lang_from_roles(roles)
        if found:
            return found

    saved = DATA["lang"].get(str(user.id))
    if saved in ("es", "en"):
        return saved

    for g in bot.guilds:
        member = g.get_member(user.id)
        if member:
            found = _lang_from_roles(member.roles)
            if found:
                return found

    if locale:
        return "es" if str(locale).lower().startswith("es") else "en"
    return "es"


def tr(interaction, key, **kw):
    lang = get_lang(interaction.user, interaction.guild, interaction.locale)
    return MSG[lang][key].format(**kw)


async def reply(interaction, content=None, **kwargs):
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


# =====================================================================
# UTILIDADES
# =====================================================================
async def is_bot_owner(user):
    if user.id in OWNER_IDS:
        return True
    try:
        return await bot.is_owner(user)
    except Exception:
        return False


def is_trusted(guild, user):
    uid = user.id
    if uid == bot.user.id or uid == guild.owner_id or uid in OWNER_IDS:
        return True
    return uid in DATA["whitelist"].get(str(guild.id), [])


def parse_user_id(raw):
    digits = re.sub(r"\D", "", raw or "")
    if 15 <= len(digits) <= 20:
        return int(digits)
    return None


def can_moderate(interaction, target):
    guild = interaction.guild
    actor = interaction.user
    if target.id in (guild.owner_id, bot.user.id, actor.id):
        return False
    if actor.id == guild.owner_id:
        return True
    return actor.top_role > target.top_role


def has_dangerous_perms(role):
    p = role.permissions
    return (p.administrator or p.manage_guild or p.manage_roles or p.manage_channels
            or p.ban_members or p.kick_members or p.manage_webhooks)


async def timeout_member(member, minutes, reason):
    try:
        await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def delete_quietly(message):
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def clean_code(text, limit=900):
    return (text or "").replace("```", "'''")[:limit]


# --- FUNCIÓN DE LOGS / AUDITORÍA ---
async def send_log(guild, title, description, color=discord.Color.blue(), thumbnail_url=None, file=None):
    if not guild:
        return
    log_channel = discord.utils.get(guild.text_channels, name="nexus-logs")
    if not log_channel:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        try:
            log_channel = await guild.create_text_channel(name="nexus-logs", overwrites=overwrites)
        except Exception:
            return

    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    embed.set_footer(text="Nexus Protect • Security Engine", icon_url=guild.icon.url if guild.icon else None)
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)

    try:
        if file:
            await log_channel.send(embed=embed, file=file)
        else:
            await log_channel.send(embed=embed)
    except Exception:
        pass


# =====================================================================
# VISTAS: TICKETS
# =====================================================================
class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Cerrar / Close", style=discord.ButtonStyle.red, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        txt = TICKET_TEXTS[get_lang(interaction.user, interaction.guild, interaction.locale)]

        if interaction.user.guild_permissions.administrator or interaction.user.name.lower() in interaction.channel.name:
            await interaction.response.send_message(txt["closing"])

            messages = []
            async for msg in interaction.channel.history(limit=500, oldest_first=True):
                time_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                messages.append(f"[{time_str}] {msg.author} ({msg.author.id}): {msg.clean_content}")

            transcript_text = f"=== TRANSCRIPCIÓN DE TICKET: {interaction.channel.name} ===\n"
            transcript_text += f"Servidor: {interaction.guild.name}\nCerrado por: {interaction.user}\n"
            transcript_text += f"Fecha: {discord.utils.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            transcript_text += "=" * 50 + "\n\n" + "\n".join(messages)

            file_data = io.BytesIO(transcript_text.encode('utf-8'))
            transcript_file_dm = discord.File(file_data, filename=f"transcript-{interaction.channel.name}.txt")

            file_data_log = io.BytesIO(transcript_text.encode('utf-8'))
            transcript_file_log = discord.File(file_data_log, filename=f"transcript-{interaction.channel.name}.txt")

            owner = interaction.guild.owner
            if owner:
                try:
                    embed_dm = discord.Embed(
                        title="📜 Transcripción de Ticket Cerrado",
                        description=f"📂 **Canal:** `{interaction.channel.name}`\n"
                                    f"👑 **Servidor:** {interaction.guild.name}\n"
                                    f"👤 **Cerrado por:** {interaction.user.mention} (`{interaction.user.id}`)\n"
                                    f"💬 **Mensajes guardados:** `{len(messages)}`",
                        color=discord.Color.gold(),
                        timestamp=discord.utils.utcnow()
                    )
                    embed_dm.set_footer(text="Nexus Protect • Automatic Transcripts")
                    await owner.send(embed=embed_dm, file=transcript_file_dm)
                except Exception:
                    pass

            await send_log(
                interaction.guild,
                "📜 Ticket Cerrado & Transcrito",
                f"📂 **Canal:** `{interaction.channel.name}`\n"
                f"👤 **Acción por:** {interaction.user.mention}\n"
                f"📄 La transcripción adjunta contiene **{len(messages)}** mensajes.",
                color=discord.Color.dark_teal(),
                file=transcript_file_log
            )

            await asyncio.sleep(4)
            await interaction.channel.delete(reason="Ticket cerrado y transcrito.")
        else:
            await interaction.response.send_message(txt["no_perm_close"], ephemeral=True)


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎟️ Crear Ticket / Open Ticket", style=discord.ButtonStyle.blurple, custom_id="create_ticket_btn")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        user = interaction.user

        user_lang = get_lang(user, guild, interaction.locale)
        txt = TICKET_TEXTS[user_lang]

        channel_name = f"ticket-{user.name}"
        existing_channel = discord.utils.get(guild.text_channels, name=channel_name)
        if existing_channel:
            await interaction.response.send_message(txt["already_open"].format(channel=existing_channel.mention), ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        category = discord.utils.get(guild.categories, name="soporte")
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            topic=f"Ticket de {user.name} ({user.id}) - Lang: {user_lang.upper()}"
        )

        embed = discord.Embed(
            title=txt["welcome_title"],
            description=txt["welcome_desc"].format(user=user.mention),
            color=discord.Color.blue()
        )
        embed.set_footer(text="Nexus Protect • Support System")

        await ticket_channel.send(embed=embed, view=CloseTicketView())
        await interaction.response.send_message(txt["created"].format(channel=ticket_channel.mention), ephemeral=True)


# =====================================================================
# VISTA: SELECCIÓN DE IDIOMA (funciona en servidor y por MD)
# =====================================================================
async def apply_language(user, lang, guild=None):
    DATA["lang"][str(user.id)] = lang
    save_data()

    keep, drop = ("Español", "English") if lang == "es" else ("English", "Español")
    targets = [guild] if guild else [g for g in bot.guilds if g.get_member(user.id)]

    for g in targets:
        member = g.get_member(user.id)
        if not member:
            continue
        role_keep = discord.utils.get(g.roles, name=keep)
        role_drop = discord.utils.get(g.roles, name=drop)
        try:
            if not role_keep:
                role_keep = await g.create_role(name=keep)
            if role_drop and role_drop in member.roles:
                await member.remove_roles(role_drop)
            await member.add_roles(role_keep)
        except discord.HTTPException:
            continue


class LanguageRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Español 🇪🇸", style=discord.ButtonStyle.primary, custom_id="role_es_btn")
    async def set_es(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await apply_language(interaction.user, "es", interaction.guild)
        await interaction.followup.send("✅ Has seleccionado el idioma **Español**.", ephemeral=True)

    @discord.ui.button(label="English 🇺🇸", style=discord.ButtonStyle.primary, custom_id="role_en_btn")
    async def set_en(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await apply_language(interaction.user, "en", interaction.guild)
        await interaction.followup.send("✅ You have selected **English** language.", ephemeral=True)


# =====================================================================
# BOT PRINCIPAL + BLOQUEO DE COMANDOS POR MD
# =====================================================================
class NexusTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            try:
                await interaction.response.send_message(tr(interaction, "dm_blocked"))
            except discord.HTTPException:
                pass
            return False
        return True


class NexusBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, tree_cls=NexusTree)
        self._synced = False

    async def setup_hook(self):
        self.add_view(TicketView())
        self.add_view(CloseTicketView())
        self.add_view(LanguageRoleView())
        if not self._synced:
            await self.tree.sync()
            self._synced = True
        print("Nexus Protect: Slash Commands, Onboarding, AntiRaid, AntiNuke, AntiPing, GBans y Backup listos.")


bot = NexusBot()


# --- ERRORES / CHECKS PERSONALIZADOS ---
class NotBotOwner(app_commands.CheckFailure):
    pass


class NotGuildOwner(app_commands.CheckFailure):
    pass


def bot_owner_only():
    async def predicate(interaction: discord.Interaction):
        if await is_bot_owner(interaction.user):
            return True
        raise NotBotOwner()
    return app_commands.check(predicate)


def guild_owner_only():
    async def predicate(interaction: discord.Interaction):
        if interaction.guild and (interaction.user.id == interaction.guild.owner_id or await is_bot_owner(interaction.user)):
            return True
        raise NotGuildOwner()
    return app_commands.check(predicate)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if interaction.guild is None:
        return  # ya se respondió con el error de MD

    if isinstance(error, app_commands.MissingPermissions):
        key = "no_perm"
    elif isinstance(error, NotBotOwner):
        key = "bot_owner_only"
    elif isinstance(error, NotGuildOwner):
        key = "guild_owner_only"
    elif isinstance(error, app_commands.CheckFailure):
        key = "no_perm"
    else:
        print(f"Error en comando: {type(error).__name__}: {error}")
        key = "generic_error"

    try:
        await reply(interaction, tr(interaction, key), ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    print(f"🛡️ Nexus Protect activo como: {bot.user.name}")
    await bot.change_presence(activity=discord.Game(name="/nexus-help | 24/7 Protected"))


# =====================================================================
# BANS GLOBALES (aplicación automática)
# =====================================================================
async def apply_gbans_to_guild(guild):
    applied = 0
    for uid, info in list(DATA["gbans"].items()):
        try:
            await guild.ban(discord.Object(id=int(uid)), reason=f"Nexus GBan: {info.get('reason', 'Sin razón')}")
            applied += 1
        except discord.HTTPException:
            pass
        await asyncio.sleep(0.5)
    if applied:
        await send_log(guild, "🌍 Bans Globales Aplicados", f"Se aplicaron **{applied}** bans globales en este servidor.", color=discord.Color.dark_red())


@bot.event
async def on_guild_join(guild):
    if DATA["gbans"]:
        asyncio.create_task(apply_gbans_to_guild(guild))


# =====================================================================
# SISTEMA ANTINUKE
# =====================================================================
async def get_executor(guild, action, target_id=None):
    try:
        async for entry in guild.audit_logs(limit=6, action=action):
            if (discord.utils.utcnow() - entry.created_at).total_seconds() > 15:
                continue
            if target_id is not None and getattr(entry.target, "id", None) != target_id:
                continue
            return entry.user
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


async def punish_executor(guild, executor, label):
    try:
        await guild.ban(executor, reason=f"Nexus AntiNuke: {label}")
        result = "⛔ Fue **baneado automáticamente**."
    except discord.HTTPException:
        result = "⚠️ No se pudo sancionar automáticamente (jerarquía de roles). **Revísalo manualmente.**"
        member = guild.get_member(executor.id)
        if member:
            try:
                safe_roles = [r for r in member.roles if not r.is_default() and (r.managed or not has_dangerous_perms(r))]
                await member.edit(roles=safe_roles, reason=f"Nexus AntiNuke: {label}")
                result = "🔒 No se pudo banear; se le **quitaron los roles peligrosos**."
            except discord.HTTPException:
                pass

    await send_log(
        guild,
        f"🚨 ANTINUKE • {label}",
        f"👤 **Responsable:** {executor.mention} (`{executor.id}`)\n"
        f"⚠️ Superó el límite de **{ANTINUKE_LIMIT}** acciones en **{ANTINUKE_WINDOW}s**.\n{result}",
        color=discord.Color.dark_red()
    )


async def antinuke_check(guild, action_key, executor, label):
    if executor is None or is_trusted(guild, executor):
        return
    key = (guild.id, executor.id, action_key)
    count = register_event(ACTION_COUNTER, key, ANTINUKE_WINDOW)
    if count >= ANTINUKE_LIMIT:
        ACTION_COUNTER.pop(key, None)
        await punish_executor(guild, executor, label)


@bot.event
async def on_guild_channel_create(channel):
    guild = channel.guild
    if not module_enabled(guild.id, "antinuke"):
        return
    executor = await get_executor(guild, discord.AuditLogAction.channel_create, channel.id)
    await antinuke_check(guild, "channel_create", executor, "Creación Masiva de Canales")


@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    if not module_enabled(guild.id, "antinuke"):
        return
    executor = await get_executor(guild, discord.AuditLogAction.channel_delete, channel.id)
    await antinuke_check(guild, "channel_delete", executor, "Eliminación Masiva de Canales")


@bot.event
async def on_guild_role_create(role):
    guild = role.guild
    if not module_enabled(guild.id, "antinuke"):
        return
    executor = await get_executor(guild, discord.AuditLogAction.role_create, role.id)
    await antinuke_check(guild, "role_create", executor, "Creación Masiva de Roles")


@bot.event
async def on_guild_role_delete(role):
    guild = role.guild
    if not module_enabled(guild.id, "antinuke"):
        return
    executor = await get_executor(guild, discord.AuditLogAction.role_delete, role.id)
    await antinuke_check(guild, "role_delete", executor, "Eliminación Masiva de Roles")


@bot.event
async def on_member_ban(guild, user):
    if not module_enabled(guild.id, "antinuke"):
        return
    executor = await get_executor(guild, discord.AuditLogAction.ban, user.id)
    await antinuke_check(guild, "ban", executor, "Baneos Masivos")


async def handle_bot_added(guild, bot_member):
    executor = await get_executor(guild, discord.AuditLogAction.bot_add, bot_member.id)
    if executor is None:
        await send_log(guild, "🤖 Bot Añadido (sin verificar)",
                       f"Se añadió el bot {bot_member.mention} (`{bot_member.id}`) pero no se pudo verificar quién lo añadió. Revisa los permisos de auditoría.",
                       color=discord.Color.orange())
        return
    if is_trusted(guild, executor):
        await send_log(guild, "🤖 Bot Añadido",
                       f"👤 **Añadido por:** {executor.mention}\n🤖 **Bot:** {bot_member.mention} (`{bot_member.id}`)",
                       color=discord.Color.blue())
        return
    try:
        await bot_member.kick(reason="Nexus AntiNuke: bot no autorizado")
        result = "👢 El bot fue **expulsado**."
    except discord.HTTPException:
        result = "⚠️ No se pudo expulsar al bot, hazlo manualmente."
    await send_log(guild, "🚨 ANTINUKE • Bot No Autorizado",
                   f"👤 **Añadido por:** {executor.mention} (`{executor.id}`)\n🤖 **Bot:** {bot_member.mention} (`{bot_member.id}`)\n{result}",
                   color=discord.Color.dark_red())


# =====================================================================
# AUDITORÍA Y LOGS EN TIEMPO REAL + ANTIRAID DE ENTRADAS
# =====================================================================
@bot.event
async def on_member_join(member):
    guild = member.guild
    uid = str(member.id)

    # Ban global
    if uid in DATA["gbans"]:
        info = DATA["gbans"][uid]
        try:
            await guild.ban(member, reason=f"Nexus GBan: {info.get('reason', 'Sin razón')}")
            await send_log(guild, "🌍 Ban Global Aplicado",
                           f"👤 **Usuario:** {member.mention} (`{member.id}`)\n📝 **Razón:** {info.get('reason', 'Sin razón')}",
                           color=discord.Color.dark_red(), thumbnail_url=member.display_avatar.url)
        except discord.HTTPException:
            await send_log(guild, "🌍 Ban Global • Falló",
                           f"No se pudo banear a {member.mention} (`{member.id}`), que está en la lista global.",
                           color=discord.Color.orange())
        return

    await send_log(
        guild,
        "📥 Nuevo Miembro",
        f"👤 **Usuario:** {member.mention} (`{member.name}`)\n🆔 **ID:** `{member.id}`\n📅 **Cuenta Creada:** <t:{int(member.created_at.timestamp())}:R>",
        color=discord.Color.green(),
        thumbnail_url=member.display_avatar.url
    )

    if member.bot:
        if module_enabled(guild.id, "antinuke"):
            await handle_bot_added(guild, member)
        return

    # AntiRaid: detección de entradas masivas
    if module_enabled(guild.id, "antiraid"):
        joins = register_event(JOIN_TRACK, guild.id, RAID_JOIN_WINDOW)
        if joins >= RAID_JOIN_LIMIT and not raid_active(guild.id):
            RAID_MODE_UNTIL[guild.id] = time.time() + RAID_MODE_SECONDS
            await send_log(
                guild,
                "🚨 ANTIRAID • Modo Raid Activado",
                f"Se detectaron **{joins}** entradas en {RAID_JOIN_WINDOW}s.\n"
                f"Durante **{RAID_MODE_SECONDS // 60} min** se expulsará a las cuentas con menos de **{RAID_MIN_ACCOUNT_AGE_DAYS} días**.",
                color=discord.Color.dark_red()
            )

        if raid_active(guild.id):
            age_days = (discord.utils.utcnow() - member.created_at).days
            if age_days < RAID_MIN_ACCOUNT_AGE_DAYS:
                try:
                    await member.kick(reason="Nexus AntiRaid: cuenta nueva durante raid")
                    await send_log(guild, "🛡️ AntiRaid • Cuenta Expulsada",
                                   f"👤 {member.mention} (`{member.id}`) — cuenta de **{age_days}** días.",
                                   color=discord.Color.orange())
                    return
                except discord.HTTPException:
                    pass

    try:
        embed = discord.Embed(
            title="👋 ¡Bienvenido a Nexus Support / Welcome to Nexus Support!",
            description="🌐 Selecciona tu idioma para configurar tu experiencia:\n\nSelect your language below to configure your experience:",
            color=discord.Color.blue()
        )
        await member.send(embed=embed, view=LanguageRoleView())
    except Exception:
        pass


@bot.event
async def on_member_remove(member):
    guild = member.guild
    await send_log(
        guild,
        "📤 Miembro Salió",
        f"👤 **Usuario:** {member.mention} (`{member.name}`)\n🆔 **ID:** `{member.id}`",
        color=discord.Color.dark_gray(),
        thumbnail_url=member.display_avatar.url
    )

    if module_enabled(guild.id, "antinuke"):
        executor = await get_executor(guild, discord.AuditLogAction.kick, member.id)
        if executor:
            await antinuke_check(guild, "kick", executor, "Expulsiones Masivas")


@bot.event
async def on_member_update(before, after):
    if before.display_avatar != after.display_avatar:
        old_avatar = before.display_avatar.url
        new_avatar = after.display_avatar.url
        await send_log(
            after.guild,
            "🖼️ Foto de Perfil Actualizada",
            f"👤 **Usuario:** {after.mention} (`{after.name}`)\n🖼️ [Avatar Anterior]({old_avatar}) ➔ [Nuevo Avatar]({new_avatar})",
            color=discord.Color.teal(),
            thumbnail_url=new_avatar
        )

    if before.nick != after.nick:
        await send_log(
            after.guild,
            "✏️ Apodo Modificado",
            f"👤 **Usuario:** {after.mention}\n📝 **Antes:** `{before.nick or 'Sin apodo'}`\n📌 **Ahora:** `{after.nick or 'Sin apodo'}`",
            color=discord.Color.gold()
        )

    if before.roles != after.roles:
        added_roles = [r.mention for r in after.roles if r not in before.roles]
        removed_roles = [r.mention for r in before.roles if r not in after.roles]

        if added_roles:
            await send_log(
                after.guild,
                "🎭 Rol Asignado",
                f"👤 **Usuario:** {after.mention}\n➕ **Rol(es):** {', '.join(added_roles)}",
                color=discord.Color.blue()
            )
        if removed_roles:
            await send_log(
                after.guild,
                "🎭 Rol Quitado",
                f"👤 **Usuario:** {after.mention}\n➖ **Rol(es):** {', '.join(removed_roles)}",
                color=discord.Color.red()
            )


@bot.event
async def on_message_delete(message):
    if message.author.bot or not message.guild:
        return

    content = clean_code(message.content) if message.content else "*Mensaje con archivos o embeds*"
    await send_log(
        message.guild,
        "🗑️ Mensaje Eliminado",
        f"👤 **Autor:** {message.author.mention} (`{message.author.name}`)\n💬 **Canal:** {message.channel.mention}\n📝 **Contenido:**\n```{content}```",
        color=discord.Color.red()
    )


@bot.event
async def on_message_edit(before, after):
    if before.author.bot or not before.guild or before.content == after.content:
        return

    await send_log(
        before.guild,
        "✏️ Mensaje Editado",
        f"👤 **Autor:** {before.author.mention} (`{before.author.name}`)\n💬 **Canal:** {before.channel.mention}\n\n🛑 **Antes:**\n```{clean_code(before.content, 800)}```\n✅ **Después:**\n```{clean_code(after.content, 800)}```",
        color=discord.Color.orange()
    )


# =====================================================================
# PROTECCIONES DE MENSAJES: ANTISPAM / ANTI PING MASIVO / ANTI INVITACIONES
# =====================================================================
INVITE_REGEX = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite|discord\.me|discord\.io)/\S+", re.IGNORECASE)


async def warn_in_channel(channel, text):
    try:
        await channel.send(text, delete_after=10)
    except discord.HTTPException:
        pass


async def run_protections(message):
    guild = message.guild
    member = message.author
    channel = message.channel
    lang = get_lang(member, guild)
    content_lower = message.content.lower()

    # --- AntiSpam ---
    if module_enabled(guild.id, "antispam"):
        key = (guild.id, member.id)
        if register_event(SPAM_TRACK, key, SPAM_WINDOW) >= SPAM_MESSAGES:
            SPAM_TRACK.pop(key, None)
            await timeout_member(member, 5, "Nexus AntiSpam")
            try:
                await channel.purge(
                    limit=50,
                    check=lambda m: m.author.id == member.id,
                    after=discord.utils.utcnow() - datetime.timedelta(seconds=30)
                )
            except discord.HTTPException:
                await delete_quietly(message)
            await warn_in_channel(channel, MSG[lang]["spam_warn"].format(user=member.mention))
            await send_log(guild, "⚠️ AntiSpam • Usuario Silenciado",
                           f"🚨 {member.mention} envió {SPAM_MESSAGES}+ mensajes en {SPAM_WINDOW}s en {channel.mention}. Timeout de 5 min.",
                           color=discord.Color.orange())
            return True

    # --- Anti Ping Masivo ---
    if module_enabled(guild.id, "antiping"):
        unique_mentions = len({m.id for m in message.mentions}) + len({r.id for r in message.role_mentions})
        everyone = message.mention_everyone or "@everyone" in content_lower or "@here" in content_lower
        total = register_event(MENTION_TRACK, (guild.id, member.id), MENTION_WINDOW, unique_mentions)
        if unique_mentions > MENTION_LIMIT or everyone or total >= MENTION_WINDOW_LIMIT:
            await delete_quietly(message)
            muted = await timeout_member(member, 10, "Nexus AntiPing: menciones masivas")
            await warn_in_channel(channel, MSG[lang]["mention_warn"].format(user=member.mention))
            await send_log(guild, "⚠️ Anti Ping Masivo",
                           f"🚨 Mensaje de {member.mention} eliminado en {channel.mention}.\n"
                           f"📊 Menciones: `{unique_mentions}` (acumuladas: `{total}`) • @everyone/@here: `{everyone}`\n"
                           f"🔇 Timeout 10 min: {'✅' if muted else '❌ (sin permisos)'}",
                           color=discord.Color.orange())
            return True

    # --- Anti Invitaciones ---
    if module_enabled(guild.id, "antiinvite") and INVITE_REGEX.search(message.content):
        await delete_quietly(message)
        await warn_in_channel(channel, MSG[lang]["invite_warn"].format(user=member.mention))
        await send_log(guild, "🛡️ Anti Invitaciones",
                       f"🚨 Se eliminó un enlace de invitación enviado por {member.mention} en {channel.mention}.",
                       color=discord.Color.red())
        return True

    return False


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Comandos por MD bloqueados
    if message.guild is None:
        if message.content.startswith(("!", "/")):
            lang = get_lang(message.author)
            try:
                await message.channel.send(MSG[lang]["dm_blocked"])
            except discord.HTTPException:
                pass
        return

    member = message.author
    exempt = (not isinstance(member, discord.Member)
              or member.guild_permissions.administrator
              or is_trusted(message.guild, member))
    if not exempt:
        if await run_protections(message):
            return

    await bot.process_commands(message)


# =====================================================================
# BACKUP DEL SERVIDOR
# =====================================================================
@bot.tree.command(name="backup-create", description="Crea una copia de seguridad completa del servidor en formato JSON.")
@app_commands.checks.has_permissions(administrator=True)
async def backup_create(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    backup_data = {
        "guild_name": guild.name,
        "created_at": discord.utils.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "roles": [],
        "categories": [],
        "channels": []
    }

    for role in reversed(guild.roles):
        if not role.is_default() and not role.managed:
            backup_data["roles"].append({
                "name": role.name,
                "color": role.color.value,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
                "permissions": role.permissions.value
            })

    for category in guild.categories:
        cat_data = {"name": category.name, "channels": []}
        for channel in category.channels:
            cat_data["channels"].append({
                "name": channel.name,
                "type": str(channel.type),
                "topic": getattr(channel, "topic", None)
            })
        backup_data["categories"].append(cat_data)

    for channel in guild.channels:
        if channel.category is None and not isinstance(channel, discord.CategoryChannel):
            backup_data["channels"].append({
                "name": channel.name,
                "type": str(channel.type),
                "topic": getattr(channel, "topic", None)
            })

    json_bytes = json.dumps(backup_data, indent=4).encode('utf-8')
    file_data = io.BytesIO(json_bytes)
    backup_file_dm = discord.File(file_data, filename=f"backup-{guild.name.replace(' ', '_')}.json")

    file_data_log = io.BytesIO(json_bytes)
    backup_file_log = discord.File(file_data_log, filename=f"backup-{guild.name.replace(' ', '_')}.json")

    try:
        embed_dm = discord.Embed(
            title="📦 Copia de Seguridad Generada",
            description=f"🏰 **Servidor:** {guild.name}\n"
                        f"🎭 **Roles guardados:** `{len(backup_data['roles'])}`\n"
                        f"📂 **Categorías:** `{len(backup_data['categories'])}`",
            color=discord.Color.green()
        )
        await interaction.user.send(embed=embed_dm, file=backup_file_dm)
        await interaction.followup.send("✅ Backup creado con éxito. Te hemos enviado el archivo `.json` por **Mensaje Directo**.", ephemeral=True)
    except Exception:
        await interaction.followup.send("⚠️ Se creó el backup, pero no pudimos enviártelo por MD. Se guardó en `#nexus-logs`.", ephemeral=True)

    await send_log(
        guild,
        "📦 Backup Creado",
        f"👤 **Administrador:** {interaction.user.mention}\n💾 El respaldo de la estructura fue generado exitosamente.",
        color=discord.Color.gold(),
        file=backup_file_log
    )


@bot.tree.command(name="backup-load", description="Carga una estructura de servidor desde un archivo JSON de respaldo.")
@app_commands.describe(archivo="Archivo .json con el backup")
@app_commands.checks.has_permissions(administrator=True)
async def backup_load(interaction: discord.Interaction, archivo: discord.Attachment):
    if not archivo.filename.endswith(".json"):
        await interaction.response.send_message("❌ El archivo debe ser un archivo `.json` válido.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    try:
        content = await archivo.read()
        data = json.loads(content.decode('utf-8'))

        for role_data in data.get("roles", []):
            if not discord.utils.get(guild.roles, name=role_data["name"]):
                await guild.create_role(
                    name=role_data["name"],
                    color=discord.Color(role_data["color"]),
                    hoist=role_data["hoist"],
                    mentionable=role_data["mentionable"]
                )

        for cat_data in data.get("categories", []):
            category = discord.utils.get(guild.categories, name=cat_data["name"])
            if not category:
                category = await guild.create_category(name=cat_data["name"])

            for ch_data in cat_data.get("channels", []):
                if ch_data["type"] == "text":
                    if not discord.utils.get(category.text_channels, name=ch_data["name"]):
                        await guild.create_text_channel(name=ch_data["name"], category=category, topic=ch_data.get("topic"))
                elif ch_data["type"] == "voice":
                    if not discord.utils.get(category.voice_channels, name=ch_data["name"]):
                        await guild.create_voice_channel(name=ch_data["name"], category=category)

        await interaction.followup.send("✅ Estructura del backup restaurada/cargada con éxito.", ephemeral=True)
        await send_log(guild, "📥 Backup Cargado", f"👤 **Administrador:** {interaction.user.mention}\nSe aplicó el archivo `{archivo.filename}`.", color=discord.Color.blue())

    except Exception as e:
        await interaction.followup.send(f"❌ Error al procesar el archivo de backup: `{e}`", ephemeral=True)


# =====================================================================
# PANELES / SETUP
# =====================================================================
@bot.tree.command(name="setup-onboarding", description="Despliega el panel para seleccionar idioma en un canal.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_onboarding(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐 Selecciona tu Idioma / Select Your Language",
        description="🇪🇸 Haz clic en tu idioma para personalizar los canales y el sistema de tickets.\n\n🇺🇸 Click on your language button below to customize channels and ticket support.",
        color=discord.Color.blue()
    )
    await interaction.channel.send(embed=embed, view=LanguageRoleView())
    await interaction.response.send_message("✅ **[OK]** Panel de selección de idioma desplegado.", ephemeral=True)


@bot.tree.command(name="setup-panel-tickets", description="Despliega el panel de soporte.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_panel_tickets(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎟️ Panel de Soporte y Apelaciones / Support Panel",
        description="👇 Presiona el botón de abajo para abrir un ticket de soporte privado.\n\nPress the button below to open a private support ticket.",
        color=discord.Color.blue()
    )
    embed.set_footer(text="Nexus Protect • Support Engine")
    await interaction.channel.send(embed=embed, view=TicketView())
    await interaction.response.send_message("✅ **[OK]** Panel de tickets desplegado.", ephemeral=True)


@bot.tree.command(name="setup-antiraid", description="Crea o configura el canal de logs de AntiRaid y auditoría.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_antiraid(interaction: discord.Interaction):
    await send_log(interaction.guild, "🛡️ Sistema de Auditoría & AntiRaid Activado", "✅ El canal de registros e historial de seguridad está 100% operativo.", color=discord.Color.green())
    await interaction.response.send_message("✅ **[OK]** Canal `#nexus-logs` configurado.", ephemeral=True)


# =====================================================================
# MODERACIÓN
# =====================================================================
@bot.tree.command(name="purge", description="Elimina una cantidad de mensajes en el canal actual.")
@app_commands.describe(cantidad="Número de mensajes a eliminar (1-100)")
@app_commands.checks.has_permissions(manage_messages=True)
async def purge(interaction: discord.Interaction, cantidad: int):
    if cantidad < 1 or cantidad > 100:
        await interaction.response.send_message("❌ **[ERROR]** Solo puedes borrar entre 1 y 100 mensajes a la vez.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=cantidad)
        await interaction.followup.send(f"🧹 Se han eliminado **{len(deleted)}** mensajes correctamente.", ephemeral=True)
        await send_log(
            interaction.guild,
            "🧹 Purge de Mensajes",
            f"🛡️ **Moderador:** {interaction.user.mention}\n💬 **Canal:** {interaction.channel.mention}\n🔢 **Cantidad:** `{len(deleted)}` mensajes.",
            color=discord.Color.purple()
        )
    except discord.Forbidden:
        await interaction.followup.send(tr(interaction, "bot_no_perm"), ephemeral=True)


@bot.tree.command(name="kick", description="Expulsa a un usuario del servidor.")
@app_commands.describe(usuario="Usuario a expulsar", razon="Razón de la expulsión")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, usuario: discord.Member, razon: str = "No especificada"):
    if not can_moderate(interaction, usuario):
        await interaction.response.send_message(tr(interaction, "hierarchy"), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await usuario.kick(reason=razon)
        await interaction.followup.send(f"👢 Usuario {usuario.mention} expulsado. Razón: {razon}", ephemeral=True)
        await send_log(interaction.guild, "👢 Kick de Usuario", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {interaction.user.mention}\n📝 **Razón:** {razon}", color=discord.Color.orange())
    except discord.Forbidden:
        await interaction.followup.send(tr(interaction, "bot_no_perm"), ephemeral=True)


@bot.tree.command(name="ban", description="Banea a un usuario del servidor.")
@app_commands.describe(usuario="Usuario a banear", razon="Razón del baneo")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, usuario: discord.Member, razon: str = "No especificada"):
    if not can_moderate(interaction, usuario):
        await interaction.response.send_message(tr(interaction, "hierarchy"), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await usuario.ban(reason=razon)
        await interaction.followup.send(f"⛔ Usuario {usuario.mention} baneado. Razón: {razon}", ephemeral=True)
        await send_log(interaction.guild, "⛔ Ban de Usuario", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {interaction.user.mention}\n📝 **Razón:** {razon}", color=discord.Color.red())
    except discord.Forbidden:
        await interaction.followup.send(tr(interaction, "bot_no_perm"), ephemeral=True)


@bot.tree.command(name="unban", description="Desbanea a un usuario por su ID.")
@app_commands.describe(usuario_id="ID del usuario", razon="Razón del desbaneo")
@app_commands.checks.has_permissions(ban_members=True)
async def unban(interaction: discord.Interaction, usuario_id: str, razon: str = "No especificada"):
    uid = parse_user_id(usuario_id)
    if uid is None:
        await interaction.response.send_message(tr(interaction, "bad_id"), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.guild.unban(discord.Object(id=uid), reason=razon)
        await interaction.followup.send(f"✅ Usuario `{uid}` desbaneado.", ephemeral=True)
        await send_log(interaction.guild, "✅ Unban de Usuario", f"🆔 **ID:** `{uid}`\n🛡️ **Moderador:** {interaction.user.mention}\n📝 **Razón:** {razon}", color=discord.Color.green())
    except discord.NotFound:
        await interaction.followup.send("❌ Ese usuario no está baneado.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(tr(interaction, "bot_no_perm"), ephemeral=True)


@bot.tree.command(name="timeout", description="Silencia temporalmente a un usuario.")
@app_commands.describe(usuario="Usuario a silenciar", minutos="Duración en minutos (1-40320)", razon="Razón")
@app_commands.checks.has_permissions(moderate_members=True)
async def timeout_cmd(interaction: discord.Interaction, usuario: discord.Member, minutos: app_commands.Range[int, 1, 40320], razon: str = "No especificada"):
    if not can_moderate(interaction, usuario):
        await interaction.response.send_message(tr(interaction, "hierarchy"), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if await timeout_member(usuario, minutos, razon):
        await interaction.followup.send(f"🔇 {usuario.mention} silenciado por **{minutos}** min. Razón: {razon}", ephemeral=True)
        await send_log(interaction.guild, "🔇 Timeout", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {interaction.user.mention}\n⏱️ **Duración:** {minutos} min\n📝 **Razón:** {razon}", color=discord.Color.orange())
    else:
        await interaction.followup.send(tr(interaction, "bot_no_perm"), ephemeral=True)


@bot.tree.command(name="untimeout", description="Quita el silencio a un usuario.")
@app_commands.describe(usuario="Usuario a des-silenciar")
@app_commands.checks.has_permissions(moderate_members=True)
async def untimeout(interaction: discord.Interaction, usuario: discord.Member):
    await interaction.response.defer(ephemeral=True)
    try:
        await usuario.timeout(None, reason=f"Untimeout por {interaction.user}")
        await interaction.followup.send(f"🔊 {usuario.mention} ya puede hablar de nuevo.", ephemeral=True)
        await send_log(interaction.guild, "🔊 Timeout Removido", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {interaction.user.mention}", color=discord.Color.green())
    except discord.HTTPException:
        await interaction.followup.send(tr(interaction, "bot_no_perm"), ephemeral=True)


@bot.tree.command(name="warn", description="Advierte a un usuario (3 advertencias = 1 hora de timeout).")
@app_commands.describe(usuario="Usuario a advertir", razon="Razón de la advertencia")
@app_commands.checks.has_permissions(moderate_members=True)
async def warn(interaction: discord.Interaction, usuario: discord.Member, razon: str):
    if not can_moderate(interaction, usuario):
        await interaction.response.send_message(tr(interaction, "hierarchy"), ephemeral=True)
        return
    await interaction.response.defer()
    guild_warns = DATA["warns"].setdefault(str(interaction.guild.id), {})
    user_warns = guild_warns.setdefault(str(usuario.id), [])
    user_warns.append({"reason": razon, "mod": interaction.user.id, "date": int(time.time())})
    save_data()

    total = len(user_warns)
    extra = ""
    if total % WARN_TIMEOUT_AT == 0:
        if await timeout_member(usuario, 60, f"Nexus: {total} advertencias"):
            extra = "\n🔇 Silenciado **1 hora** por acumular advertencias."

    await interaction.followup.send(f"⚠️ {usuario.mention} fue advertido (**{total}** en total).\n📝 Razón: {razon}{extra}")
    await send_log(interaction.guild, "⚠️ Advertencia", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {interaction.user.mention}\n📝 **Razón:** {razon}\n🔢 **Total:** {total}", color=discord.Color.gold())


@bot.tree.command(name="warns", description="Muestra las advertencias de un usuario.")
@app_commands.describe(usuario="Usuario a consultar")
@app_commands.checks.has_permissions(moderate_members=True)
async def warns(interaction: discord.Interaction, usuario: discord.Member):
    user_warns = DATA["warns"].get(str(interaction.guild.id), {}).get(str(usuario.id), [])
    if not user_warns:
        await interaction.response.send_message(f"✅ {usuario.mention} no tiene advertencias.", ephemeral=True)
        return
    lines = [f"`{i}.` <t:{w['date']}:R> — {w['reason']} (por <@{w['mod']}>)" for i, w in enumerate(user_warns[-10:], start=max(1, len(user_warns) - 9))]
    embed = discord.Embed(title=f"⚠️ Advertencias de {usuario}", description="\n".join(lines), color=discord.Color.gold())
    embed.set_footer(text=f"Total: {len(user_warns)}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clearwarns", description="Borra todas las advertencias de un usuario.")
@app_commands.describe(usuario="Usuario")
@app_commands.checks.has_permissions(moderate_members=True)
async def clearwarns(interaction: discord.Interaction, usuario: discord.Member):
    guild_warns = DATA["warns"].get(str(interaction.guild.id), {})
    removed = guild_warns.pop(str(usuario.id), [])
    save_data()
    await interaction.response.send_message(f"🧽 Se borraron **{len(removed)}** advertencias de {usuario.mention}.", ephemeral=True)
    await send_log(interaction.guild, "🧽 Advertencias Borradas", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {interaction.user.mention}\n🔢 **Borradas:** {len(removed)}", color=discord.Color.green())


@bot.tree.command(name="slowmode", description="Cambia el modo lento del canal actual (0 = desactivar).")
@app_commands.describe(segundos="Segundos entre mensajes (0-21600)")
@app_commands.checks.has_permissions(manage_channels=True)
async def slowmode(interaction: discord.Interaction, segundos: app_commands.Range[int, 0, 21600]):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message(tr(interaction, "not_text"), ephemeral=True)
        return
    try:
        await interaction.channel.edit(slowmode_delay=segundos, reason=f"Slowmode por {interaction.user}")
        await interaction.response.send_message(f"🐢 Modo lento: **{segundos}s**." if segundos else "🐇 Modo lento desactivado.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(tr(interaction, "bot_no_perm"), ephemeral=True)


@bot.tree.command(name="lock", description="Bloquea el canal actual para que nadie pueda escribir.")
@app_commands.describe(razon="Razón del bloqueo")
@app_commands.checks.has_permissions(manage_channels=True)
async def lock(interaction: discord.Interaction, razon: str = "No especificada"):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(tr(interaction, "not_text"), ephemeral=True)
        return
    role = interaction.guild.default_role
    overwrite = channel.overwrites_for(role)
    overwrite.send_messages = False
    try:
        await channel.set_permissions(role, overwrite=overwrite, reason=f"Lock: {razon}")
        await interaction.response.send_message(f"🔒 Canal bloqueado. Razón: {razon}")
        await send_log(interaction.guild, "🔒 Canal Bloqueado", f"💬 **Canal:** {channel.mention}\n🛡️ **Moderador:** {interaction.user.mention}\n📝 **Razón:** {razon}", color=discord.Color.orange())
    except discord.Forbidden:
        await interaction.response.send_message(tr(interaction, "bot_no_perm"), ephemeral=True)


@bot.tree.command(name="unlock", description="Desbloquea el canal actual.")
@app_commands.checks.has_permissions(manage_channels=True)
async def unlock(interaction: discord.Interaction):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(tr(interaction, "not_text"), ephemeral=True)
        return
    role = interaction.guild.default_role
    overwrite = channel.overwrites_for(role)
    overwrite.send_messages = None
    try:
        await channel.set_permissions(role, overwrite=None if overwrite.is_empty() else overwrite, reason=f"Unlock por {interaction.user}")
        await interaction.response.send_message("🔓 Canal desbloqueado.")
        await send_log(interaction.guild, "🔓 Canal Desbloqueado", f"💬 **Canal:** {channel.mention}\n🛡️ **Moderador:** {interaction.user.mention}", color=discord.Color.green())
    except discord.Forbidden:
        await interaction.response.send_message(tr(interaction, "bot_no_perm"), ephemeral=True)


@bot.tree.command(name="lockdown", description="Bloquea TODOS los canales de texto del servidor (emergencia).")
@app_commands.describe(razon="Razón del lockdown")
@app_commands.checks.has_permissions(administrator=True)
async def lockdown(interaction: discord.Interaction, razon: str = "Emergencia"):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role = guild.default_role
    saved = DATA["lockdown"].setdefault(str(guild.id), {})
    count = 0
    for channel in guild.text_channels:
        overwrite = channel.overwrites_for(role)
        if overwrite.send_messages is False:
            continue
        previous = overwrite.send_messages
        overwrite.send_messages = False
        try:
            await channel.set_permissions(role, overwrite=overwrite, reason=f"Nexus Lockdown: {razon}")
            saved[str(channel.id)] = previous
            count += 1
        except discord.HTTPException:
            continue
    save_data()
    await interaction.followup.send(f"🚨 Lockdown activo: **{count}** canales bloqueados. Usa `/unlockdown` para restaurarlos.", ephemeral=True)
    await send_log(guild, "🚨 LOCKDOWN Activado", f"🛡️ **Admin:** {interaction.user.mention}\n📝 **Razón:** {razon}\n🔒 **Canales bloqueados:** {count}", color=discord.Color.dark_red())


@bot.tree.command(name="unlockdown", description="Restaura los canales bloqueados por /lockdown.")
@app_commands.checks.has_permissions(administrator=True)
async def unlockdown(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    role = guild.default_role
    saved = DATA["lockdown"].pop(str(guild.id), {})
    count = 0
    for channel_id, previous in saved.items():
        channel = guild.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            continue
        overwrite = channel.overwrites_for(role)
        overwrite.send_messages = previous
        try:
            await channel.set_permissions(role, overwrite=None if overwrite.is_empty() else overwrite, reason="Nexus Unlockdown")
            count += 1
        except discord.HTTPException:
            continue
    save_data()
    await interaction.followup.send(f"✅ Lockdown finalizado: **{count}** canales restaurados.", ephemeral=True)
    await send_log(guild, "✅ Lockdown Finalizado", f"🛡️ **Admin:** {interaction.user.mention}\n🔓 **Canales restaurados:** {count}", color=discord.Color.green())


@bot.tree.command(name="raid-mode", description="Activa o desactiva manualmente el modo raid.")
@app_commands.describe(activar="True para activar (15 min), False para desactivar")
@app_commands.checks.has_permissions(administrator=True)
async def raid_mode(interaction: discord.Interaction, activar: bool):
    if activar:
        RAID_MODE_UNTIL[interaction.guild.id] = time.time() + 900
        msg = f"🚨 Modo raid **activado** por 15 minutos: se expulsa a cuentas con menos de {RAID_MIN_ACCOUNT_AGE_DAYS} días."
    else:
        RAID_MODE_UNTIL.pop(interaction.guild.id, None)
        msg = "✅ Modo raid **desactivado**."
    await interaction.response.send_message(msg, ephemeral=True)
    await send_log(interaction.guild, "🚨 Modo Raid (manual)", f"🛡️ **Admin:** {interaction.user.mention}\n{msg}", color=discord.Color.orange())


# =====================================================================
# CONFIGURACIÓN DE PROTECCIONES + WHITELIST
# =====================================================================
@bot.tree.command(name="config-proteccion", description="Activa o desactiva un módulo de protección.")
@app_commands.describe(modulo="Módulo a configurar", estado="True = activado, False = desactivado")
@app_commands.choices(modulo=[app_commands.Choice(name=name, value=key) for key, name in MODULES.items()])
@app_commands.checks.has_permissions(administrator=True)
async def config_proteccion(interaction: discord.Interaction, modulo: app_commands.Choice[str], estado: bool):
    DATA["config"].setdefault(str(interaction.guild.id), {})[modulo.value] = estado
    save_data()
    await interaction.response.send_message(f"{'🟢' if estado else '🔴'} **{modulo.name}** ahora está **{'activado' if estado else 'desactivado'}**.", ephemeral=True)
    await send_log(interaction.guild, "⚙️ Configuración de Protección", f"🛡️ **Admin:** {interaction.user.mention}\n📦 **Módulo:** {modulo.name}\n📌 **Estado:** {'Activado' if estado else 'Desactivado'}", color=discord.Color.blue())


whitelist_group = app_commands.Group(name="whitelist", description="Usuarios de confianza que el AntiNuke y las protecciones ignoran.")
bot.tree.add_command(whitelist_group)


@whitelist_group.command(name="add", description="Añade un usuario de confianza (solo dueño del servidor).")
@app_commands.describe(usuario="Usuario a añadir")
@guild_owner_only()
async def whitelist_add(interaction: discord.Interaction, usuario: discord.User):
    wl = DATA["whitelist"].setdefault(str(interaction.guild.id), [])
    if usuario.id not in wl:
        wl.append(usuario.id)
        save_data()
    await interaction.response.send_message(f"✅ {usuario.mention} añadido a la whitelist.", ephemeral=True)
    await send_log(interaction.guild, "✅ Whitelist • Añadido", f"👤 {usuario.mention} (`{usuario.id}`)\n🛡️ **Por:** {interaction.user.mention}", color=discord.Color.green())


@whitelist_group.command(name="remove", description="Quita un usuario de la whitelist (solo dueño del servidor).")
@app_commands.describe(usuario="Usuario a quitar")
@guild_owner_only()
async def whitelist_remove(interaction: discord.Interaction, usuario: discord.User):
    wl = DATA["whitelist"].setdefault(str(interaction.guild.id), [])
    if usuario.id in wl:
        wl.remove(usuario.id)
        save_data()
    await interaction.response.send_message(f"🗑️ {usuario.mention} quitado de la whitelist.", ephemeral=True)
    await send_log(interaction.guild, "🗑️ Whitelist • Quitado", f"👤 {usuario.mention} (`{usuario.id}`)\n🛡️ **Por:** {interaction.user.mention}", color=discord.Color.orange())


@whitelist_group.command(name="list", description="Muestra los usuarios de confianza.")
@app_commands.checks.has_permissions(administrator=True)
async def whitelist_list(interaction: discord.Interaction):
    wl = DATA["whitelist"].get(str(interaction.guild.id), [])
    text = "\n".join(f"• <@{uid}> (`{uid}`)" for uid in wl) or "Nadie (aparte del dueño del servidor)."
    await interaction.response.send_message(embed=discord.Embed(title="✅ Whitelist", description=text, color=discord.Color.green()), ephemeral=True)


# =====================================================================
# BANS GLOBALES (solo dueño del bot)
# =====================================================================
@bot.tree.command(name="gban", description="Ban global: banea a un usuario en TODOS los servidores con Nexus (solo dueño del bot).")
@app_commands.describe(usuario_id="ID del usuario", razon="Razón del ban global")
@bot_owner_only()
async def gban(interaction: discord.Interaction, usuario_id: str, razon: str = "No especificada"):
    uid = parse_user_id(usuario_id)
    if uid is None:
        await interaction.response.send_message(tr(interaction, "bad_id"), ephemeral=True)
        return
    if uid in (bot.user.id, interaction.user.id) or uid in OWNER_IDS:
        await interaction.response.send_message(tr(interaction, "hierarchy"), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    DATA["gbans"][str(uid)] = {"reason": razon, "by": interaction.user.id, "date": int(time.time())}
    save_data()

    total = len(bot.guilds)
    ok = 0
    for g in list(bot.guilds):
        try:
            await g.ban(discord.Object(id=uid), reason=f"Nexus GBan: {razon}")
            ok += 1
            await send_log(g, "🌍 Ban Global", f"🆔 **ID:** `{uid}`\n📝 **Razón:** {razon}", color=discord.Color.dark_red())
        except discord.HTTPException:
            pass
    await interaction.followup.send(f"🌍 `{uid}` añadido a la lista global y baneado en **{ok}/{total}** servidores. Si entra a otro servidor con Nexus, será baneado automáticamente.", ephemeral=True)


@bot.tree.command(name="ungban", description="Quita un ban global y desbanea donde se aplicó (solo dueño del bot).")
@app_commands.describe(usuario_id="ID del usuario")
@bot_owner_only()
async def ungban(interaction: discord.Interaction, usuario_id: str):
    uid = parse_user_id(usuario_id)
    if uid is None:
        await interaction.response.send_message(tr(interaction, "bad_id"), ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    existed = DATA["gbans"].pop(str(uid), None) is not None
    save_data()

    unbanned = 0
    for g in list(bot.guilds):
        try:
            entry = await g.fetch_ban(discord.Object(id=uid))
            if entry.reason and entry.reason.startswith("Nexus GBan"):
                await g.unban(discord.Object(id=uid), reason="Nexus: ban global retirado")
                unbanned += 1
        except discord.HTTPException:
            pass
    await interaction.followup.send(
        f"{'✅' if existed else 'ℹ️'} `{uid}` {'retirado de la lista global' if existed else 'no estaba en la lista global'}. Desbaneado en **{unbanned}** servidores.",
        ephemeral=True
    )


@bot.tree.command(name="gban-list", description="Muestra la lista de bans globales (solo dueño del bot).")
@bot_owner_only()
async def gban_list(interaction: discord.Interaction):
    if not DATA["gbans"]:
        await interaction.response.send_message("✅ La lista de bans globales está vacía.", ephemeral=True)
        return
    lines = [f"`{uid}` — {info.get('reason', 'Sin razón')}" for uid, info in list(DATA["gbans"].items())[:25]]
    embed = discord.Embed(title="🌍 Bans Globales", description="\n".join(lines), color=discord.Color.dark_red())
    embed.set_footer(text=f"Total: {len(DATA['gbans'])}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =====================================================================
# INFORMACIÓN
# =====================================================================
@bot.tree.command(name="userinfo", description="Muestra información de un usuario.")
@app_commands.describe(usuario="Usuario a consultar (por defecto, tú)")
async def userinfo(interaction: discord.Interaction, usuario: discord.Member = None):
    usuario = usuario or interaction.user
    roles = [r.mention for r in reversed(usuario.roles) if not r.is_default()][:10]
    warns_count = len(DATA["warns"].get(str(interaction.guild.id), {}).get(str(usuario.id), []))
    embed = discord.Embed(title=f"👤 {usuario}", color=usuario.color)
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.add_field(name="🆔 ID", value=f"`{usuario.id}`")
    embed.add_field(name="📅 Cuenta creada", value=f"<t:{int(usuario.created_at.timestamp())}:R>")
    if usuario.joined_at:
        embed.add_field(name="📥 Se unió", value=f"<t:{int(usuario.joined_at.timestamp())}:R>")
    embed.add_field(name="⚠️ Advertencias", value=str(warns_count))
    embed.add_field(name="🎭 Roles", value=" ".join(roles) or "Ninguno", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="serverinfo", description="Muestra información del servidor.")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=f"🏰 {g.name}", color=discord.Color.blue())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="👑 Dueño", value=f"<@{g.owner_id}>")
    embed.add_field(name="👥 Miembros", value=str(g.member_count))
    embed.add_field(name="💬 Canales", value=str(len(g.channels)))
    embed.add_field(name="🎭 Roles", value=str(len(g.roles)))
    embed.add_field(name="📅 Creado", value=f"<t:{int(g.created_at.timestamp())}:R>")
    embed.add_field(name="🆔 ID", value=f"`{g.id}`")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="nexus-status", description="Muestra el estado de Nexus Protect.")
async def nexus_status(interaction: discord.Interaction):
    g = interaction.guild
    modules = "\n".join(f"{'🟢' if module_enabled(g.id, key) else '🔴'} **{name}**" for key, name in MODULES.items())
    embed = discord.Embed(
        title="🛡️ Nexus Protect • Estado del Sistema",
        description="🟢 **Estado:** En línea y operativo 24/7\n"
                    "📜 **Transcripciones:** Activas (Envío por MD al dueño)\n"
                    "📦 **Backup System:** Activo (`/backup-create`, `/backup-load`)\n"
                    "🕵️ **Auditoría:** Logs en tiempo real activos\n"
                    f"🌍 **Bans globales:** `{len(DATA['gbans'])}` usuarios\n"
                    f"✅ **Whitelist:** `{len(DATA['whitelist'].get(str(g.id), []))}` usuarios\n"
                    f"🚨 **Modo raid:** {'ACTIVO' if raid_active(g.id) else 'inactivo'}",
        color=discord.Color.green()
    )
    embed.add_field(name="Módulos de protección", value=modules, inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nexus-help", description="Lista de comandos de Nexus Protect.")
async def nexus_help(interaction: discord.Interaction):
    embed = discord.Embed(title="🛡️ Nexus Protect • Comandos", color=discord.Color.blue())
    embed.add_field(name="⚔️ Moderación", value="`/kick` `/ban` `/unban` `/timeout` `/untimeout` `/warn` `/warns` `/clearwarns` `/purge` `/slowmode`", inline=False)
    embed.add_field(name="🔒 Protección", value="`/lock` `/unlock` `/lockdown` `/unlockdown` `/raid-mode` `/config-proteccion` `/whitelist add|remove|list`", inline=False)
    embed.add_field(name="🌍 Dueño del bot", value="`/gban` `/ungban` `/gban-list`", inline=False)
    embed.add_field(name="⚙️ Sistema", value="`/setup-onboarding` `/setup-panel-tickets` `/setup-antiraid` `/backup-create` `/backup-load`", inline=False)
    embed.add_field(name="ℹ️ Info", value="`/userinfo` `/serverinfo` `/nexus-status`", inline=False)
    embed.set_footer(text="Los comandos solo funcionan dentro de servidores, no por MD.")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =====================================================================
# SERVIDOR WEB INTERNO PARA RENDER
# =====================================================================
async def handle_ping(request):
    return web.Response(text="Nexus Protect is Online 24/7!")


async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Servidor Web activo en el puerto {port}")


# =====================================================================
# INICIALIZACIÓN PRINCIPAL (con reintentos si Discord/Cloudflare bloquea la IP)
# =====================================================================
async def main():
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise ValueError("Error: No se encontró 'DISCORD_TOKEN' en las Variables de Entorno de Render.")

    await start_web_server()

    espera = 60
    while True:
        try:
            async with bot:
                await bot.start(TOKEN)
            break
        except discord.LoginFailure:
            print("Token inválido. Revisa DISCORD_TOKEN en Render.")
            break
        except discord.HTTPException as e:
            if e.status == 429:
                print(f"Discord/Cloudflare bloqueó la IP (429). Reintentando en {espera}s...")
            else:
                print(f"Error HTTP {e.status} al iniciar. Reintentando en {espera}s...")
        except Exception as e:
            print(f"Error al iniciar bot: {type(e).__name__}. Reintentando en {espera}s...")

        await asyncio.sleep(espera)
        espera = min(espera * 2, 900)
        bot.clear()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
