import os
import io
import re
import json
import time
import random
import unicodedata
import datetime
import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
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
    "antiroles": "Anti Escalada de Permisos (roles)",
    "antiscam": "Anti Scam (enlaces de phishing)",
    "wordfilter": "Filtro de Palabras",
    "levels": "Niveles y XP",
}

MODULE_DEFAULTS = {"levels": False}  # el resto de módulos vienen activados por defecto

# =====================================================================
# DATOS PERSISTENTES (JSON)
# Nota: en Render gratis el disco se borra en cada deploy.
# =====================================================================
DEFAULT_DATA = {"gbans": {}, "whitelist": {}, "warns": {}, "lang": {}, "config": {}, "lockdown": {}, "settings": {},
                "levels": {}, "giveaways": {}, "cases": {}, "words": {}, "tempvoice": {}}


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


def write_local():
    try:
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(DATA, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
    except OSError as e:
        print(f"No se pudieron guardar los datos: {e}")


# ---------------------------------------------------------------------
# RESPALDO AUTOMÁTICO EN DISCORD (los datos sobreviven a los redeploys)
# El bot guarda nexus_data.json por MD al dueño del bot (o en el canal de
# DATA_CHANNEL_ID, si lo defines) y lo recupera solo al arrancar.
# ---------------------------------------------------------------------
BACKUP_FILENAME = "nexus_data.json"
BACKUP_STATE = {"channel": None, "msg_id": None, "dirty": False, "task": None}


async def get_backup_channel():
    if BACKUP_STATE["channel"]:
        return BACKUP_STATE["channel"]
    try:
        raw_channel = os.getenv("DATA_CHANNEL_ID", "").strip()
        if raw_channel.isdigit():
            channel = bot.get_channel(int(raw_channel)) or await bot.fetch_channel(int(raw_channel))
        else:
            owner_id = next(iter(OWNER_IDS), None)
            if owner_id is None:
                info = await bot.application_info()
                owner_id = info.team.owner.id if info.team else info.owner.id
            user = bot.get_user(owner_id) or await bot.fetch_user(owner_id)
            channel = user.dm_channel or await user.create_dm()
        BACKUP_STATE["channel"] = channel
        return channel
    except Exception as e:
        print(f"Respaldo en Discord: no se pudo abrir el canal ({type(e).__name__}).")
        return None


async def restore_data_from_discord():
    try:
        channel = await get_backup_channel()
        if not channel:
            return
        async for msg in channel.history(limit=50):
            if msg.author.id != bot.user.id:
                continue
            for att in msg.attachments:
                if att.filename != BACKUP_FILENAME:
                    continue
                data = json.loads((await att.read()).decode("utf-8"))
                if not isinstance(data, dict):
                    continue
                for key in DEFAULT_DATA:
                    data.setdefault(key, {})
                DATA.clear()
                DATA.update(data)
                BACKUP_STATE["msg_id"] = msg.id
                write_local()
                print("💾 Datos restaurados desde el respaldo de Discord.")
                return
        print("💾 No hay respaldo previo en Discord (se creará al guardar datos).")
    except Exception as e:
        print(f"Respaldo en Discord: no se pudo restaurar ({type(e).__name__}).")


async def upload_backup():
    channel = await get_backup_channel()
    if not channel:
        return
    payload = json.dumps(DATA, ensure_ascii=False).encode("utf-8")
    try:
        msg = await channel.send(
            content="💾 Respaldo automático de Nexus Protect (no borres este mensaje).",
            file=discord.File(io.BytesIO(payload), filename=BACKUP_FILENAME)
        )
    except discord.HTTPException as e:
        print(f"Respaldo en Discord: no se pudo subir ({e.status}).")
        return
    old_id = BACKUP_STATE["msg_id"]
    BACKUP_STATE["msg_id"] = msg.id
    if old_id:
        try:
            await channel.get_partial_message(old_id).delete()
        except discord.HTTPException:
            pass


async def backup_worker():
    while BACKUP_STATE["dirty"]:
        BACKUP_STATE["dirty"] = False
        await asyncio.sleep(5)  # agrupa varios guardados seguidos en uno solo
        await upload_backup()


def schedule_backup():
    BACKUP_STATE["dirty"] = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = BACKUP_STATE["task"]
    if task is None or task.done():
        BACKUP_STATE["task"] = loop.create_task(backup_worker())


def save_data():
    write_local()
    schedule_backup()


def module_enabled(guild_id, module):
    return DATA["config"].get(str(guild_id), {}).get(module, MODULE_DEFAULTS.get(module, True))


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
    # Funciona con Interaction (slash) y con Context (prefijo)
    guild = interaction.guild
    actor = getattr(interaction, "user", None) or interaction.author
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
    log_channel = None
    custom_id = DATA["settings"].get(str(guild.id), {}).get("log_channel")
    if custom_id:
        log_channel = guild.get_channel(int(custom_id))
    if not log_channel:
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
        super().__init__(
            command_prefix=commands.when_mentioned_or("N!", "n!"),
            intents=intents,
            tree_cls=NexusTree,
            help_command=None,
            case_insensitive=True,
            strip_after_prefix=True,
        )
        self._synced = False

    async def setup_hook(self):
        await restore_data_from_discord()
        self.add_view(TicketView())
        self.add_view(CloseTicketView())
        self.add_view(LanguageRoleView())
        self.add_view(VerifyView())
        self.add_view(GiveawayView())
        self.add_dynamic_items(RoleButton)
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
    await start_extra_loops()


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

    if await check_min_age(member):
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

    await handle_welcome(member)

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

    await handle_goodbye(member)

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
        await check_dangerous_role_grant(before, after)

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

    SNIPE_CACHE[message.channel.id] = {
        "author": str(message.author),
        "avatar": message.author.display_avatar.url,
        "content": (message.content or "*Archivos o embeds*")[:1500],
        "ts": int(time.time()),
    }
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

    # --- AntiScam / Filtro de palabras ---
    if await extra_message_checks(message):
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

    # Comandos por MD bloqueados (solo responde si el comando existe)
    if message.guild is None:
        ctx = await bot.get_context(message)
        if ctx.valid:
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

    await award_xp(message)
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
        add_case(interaction.guild.id, usuario.id, interaction.user.id, "kick", razon)
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
        add_case(interaction.guild.id, usuario.id, interaction.user.id, "ban", razon)
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
        add_case(interaction.guild.id, uid, interaction.user.id, "unban", razon)
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
        add_case(interaction.guild.id, usuario.id, interaction.user.id, "timeout", f"{razon} ({minutos} min)")
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


@bot.tree.command(name="warn", description="Advierte a un usuario (3 = timeout, 5 = kick, 7 = ban).")
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
    extra = await warn_escalation(interaction.guild, usuario, total)

    await interaction.followup.send(f"⚠️ {usuario.mention} fue advertido (**{total}** en total).\n📝 Razón: {razon}{extra}")
    add_case(interaction.guild.id, usuario.id, interaction.user.id, "warn", razon)
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


# =====================================================================
# COMANDOS CON PREFIJO:  N!comando   o   @Nexus Protect comando
# (los comandos inexistentes se ignoran sin responder)
# =====================================================================
PMSG = {
    "es": {
        "bad_usage": "❌ **[ERROR]** Uso incorrecto. Uso correcto: `N!{usage}`",
        "bad_module": "❌ **[ERROR]** Módulo inválido. Opciones: {options}",
    },
    "en": {
        "bad_usage": "❌ **[ERROR]** Wrong usage. Correct usage: `N!{usage}`",
        "bad_module": "❌ **[ERROR]** Invalid module. Options: {options}",
    }
}


def pmsg(ctx, key, **kw):
    lang = get_lang(ctx.author, ctx.guild)
    table = MSG[lang] if key in MSG[lang] else PMSG[lang]
    return table[key].format(**kw)


async def say(ctx, content=None, **kwargs):
    kwargs.setdefault("allowed_mentions", discord.AllowedMentions.none())
    if content and "embed" not in kwargs and "embeds" not in kwargs:
        pretty = discord.Embed(description=content, color=color_for_text(content))
        pretty.set_footer(text="Nexus Protect")
        kwargs["embed"] = pretty
        content = None
    try:
        return await ctx.send(content, **kwargs)
    except discord.HTTPException:
        return None


class PNotBotOwner(commands.CheckFailure):
    pass


class PNotGuildOwner(commands.CheckFailure):
    pass


def p_bot_owner_only():
    async def predicate(ctx):
        if await is_bot_owner(ctx.author):
            return True
        raise PNotBotOwner()
    return commands.check(predicate)


def p_guild_owner_only():
    async def predicate(ctx):
        if ctx.guild and (ctx.author.id == ctx.guild.owner_id or await is_bot_owner(ctx.author)):
            return True
        raise PNotGuildOwner()
    return commands.check(predicate)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return  # No responder a comandos inexistentes

    if isinstance(error, commands.CommandInvokeError):
        error = error.original

    if isinstance(error, PNotBotOwner):
        key = "bot_owner_only"
    elif isinstance(error, PNotGuildOwner):
        key = "guild_owner_only"
    elif isinstance(error, commands.MissingPermissions):
        key = "no_perm"
    elif isinstance(error, commands.UserInputError):
        usage = f"{ctx.command.qualified_name} {ctx.command.signature}".strip() if ctx.command else ""
        await say(ctx, pmsg(ctx, "bad_usage", usage=usage))
        return
    elif isinstance(error, commands.NoPrivateMessage):
        return
    elif isinstance(error, commands.CheckFailure):
        key = "no_perm"
    elif isinstance(error, discord.Forbidden):
        key = "bot_no_perm"
    else:
        print(f"Error en comando prefijo: {type(error).__name__}: {error}")
        key = "generic_error"

    await say(ctx, pmsg(ctx, key))


# --- Funciones auxiliares compartidas por los comandos con prefijo ---
async def apply_warn(guild, usuario, mod, razon):
    user_warns = DATA["warns"].setdefault(str(guild.id), {}).setdefault(str(usuario.id), [])
    user_warns.append({"reason": razon, "mod": mod.id, "date": int(time.time())})
    save_data()
    total = len(user_warns)
    extra = await warn_escalation(guild, usuario, total)
    return total, extra


async def do_lockdown(guild, razon):
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
    return count


async def do_unlockdown(guild):
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
    return count


async def do_gban(uid, razon, by_id):
    DATA["gbans"][str(uid)] = {"reason": razon, "by": by_id, "date": int(time.time())}
    save_data()
    ok = 0
    for g in list(bot.guilds):
        try:
            await g.ban(discord.Object(id=uid), reason=f"Nexus GBan: {razon}")
            ok += 1
            await send_log(g, "🌍 Ban Global", f"🆔 **ID:** `{uid}`\n📝 **Razón:** {razon}", color=discord.Color.dark_red())
        except discord.HTTPException:
            pass
    return ok, len(bot.guilds)


async def do_ungban(uid):
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
    return existed, unbanned


def build_status_embed(g):
    modules = "\n".join(f"{'🟢' if module_enabled(g.id, key) else '🔴'} **{name}**" for key, name in MODULES.items())
    embed = discord.Embed(
        title="🛡️ Nexus Protect • Estado del Sistema",
        description="🟢 **Estado:** En línea y operativo 24/7\n"
                    "📜 **Transcripciones:** Activas (Envío por MD al dueño)\n"
                    "📦 **Backup System:** Activo (`backup-create`, `backup-load`)\n"
                    "🕵️ **Auditoría:** Logs en tiempo real activos\n"
                    f"🌍 **Bans globales:** `{len(DATA['gbans'])}` usuarios\n"
                    f"✅ **Whitelist:** `{len(DATA['whitelist'].get(str(g.id), []))}` usuarios\n"
                    f"🚨 **Modo raid:** {'ACTIVO' if raid_active(g.id) else 'inactivo'}",
        color=discord.Color.green()
    )
    embed.add_field(name="Módulos de protección", value=modules, inline=False)
    return embed


def build_backup_data(guild):
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
    return backup_data


async def restore_backup(guild, data):
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


# --- Ayuda / info ---
@bot.command(name="status", aliases=["estado", "nexus-status"])
async def p_status(ctx):
    await ctx.send(embed=build_status_embed(ctx.guild))


@bot.command(name="ping")
async def p_ping(ctx):
    await say(ctx, f"🏓 Pong! `{round(bot.latency * 1000)}ms`")


@bot.command(name="userinfo", aliases=["ui", "whois"])
async def p_userinfo(ctx, usuario: discord.Member = None):
    usuario = usuario or ctx.author
    roles = [r.mention for r in reversed(usuario.roles) if not r.is_default()][:10]
    warns_count = len(DATA["warns"].get(str(ctx.guild.id), {}).get(str(usuario.id), []))
    embed = discord.Embed(title=f"👤 {usuario}", color=usuario.color)
    embed.set_thumbnail(url=usuario.display_avatar.url)
    embed.add_field(name="🆔 ID", value=f"`{usuario.id}`")
    embed.add_field(name="📅 Cuenta creada", value=f"<t:{int(usuario.created_at.timestamp())}:R>")
    if usuario.joined_at:
        embed.add_field(name="📥 Se unió", value=f"<t:{int(usuario.joined_at.timestamp())}:R>")
    embed.add_field(name="⚠️ Advertencias", value=str(warns_count))
    embed.add_field(name="🎭 Roles", value=" ".join(roles) or "Ninguno", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="serverinfo", aliases=["si", "guildinfo"])
async def p_serverinfo(ctx):
    g = ctx.guild
    embed = discord.Embed(title=f"🏰 {g.name}", color=discord.Color.blue())
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="👑 Dueño", value=f"<@{g.owner_id}>")
    embed.add_field(name="👥 Miembros", value=str(g.member_count))
    embed.add_field(name="💬 Canales", value=str(len(g.channels)))
    embed.add_field(name="🎭 Roles", value=str(len(g.roles)))
    embed.add_field(name="📅 Creado", value=f"<t:{int(g.created_at.timestamp())}:R>")
    embed.add_field(name="🆔 ID", value=f"`{g.id}`")
    await ctx.send(embed=embed)


# --- Moderación ---
@bot.command(name="kick", aliases=["expulsar"])
@commands.has_permissions(kick_members=True)
async def p_kick(ctx, usuario: discord.Member, *, razon: str = "No especificada"):
    if not can_moderate(ctx, usuario):
        await say(ctx, pmsg(ctx, "hierarchy"))
        return
    try:
        await usuario.kick(reason=f"{razon} (por {ctx.author})")
    except discord.Forbidden:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    await say(ctx, f"👢 Usuario {usuario.mention} expulsado. Razón: {razon}")
    add_case(ctx.guild.id, usuario.id, ctx.author.id, "kick", razon)
    await send_log(ctx.guild, "👢 Kick de Usuario", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {ctx.author.mention}\n📝 **Razón:** {razon}", color=discord.Color.orange())


@bot.command(name="ban", aliases=["banear"])
@commands.has_permissions(ban_members=True)
async def p_ban(ctx, usuario: discord.Member, *, razon: str = "No especificada"):
    if not can_moderate(ctx, usuario):
        await say(ctx, pmsg(ctx, "hierarchy"))
        return
    try:
        await usuario.ban(reason=f"{razon} (por {ctx.author})")
    except discord.Forbidden:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    await say(ctx, f"⛔ Usuario {usuario.mention} baneado. Razón: {razon}")
    add_case(ctx.guild.id, usuario.id, ctx.author.id, "ban", razon)
    await send_log(ctx.guild, "⛔ Ban de Usuario", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {ctx.author.mention}\n📝 **Razón:** {razon}", color=discord.Color.red())


@bot.command(name="unban", aliases=["desbanear"])
@commands.has_permissions(ban_members=True)
async def p_unban(ctx, usuario_id: str, *, razon: str = "No especificada"):
    uid = parse_user_id(usuario_id)
    if uid is None:
        await say(ctx, pmsg(ctx, "bad_id"))
        return
    try:
        await ctx.guild.unban(discord.Object(id=uid), reason=f"{razon} (por {ctx.author})")
    except discord.NotFound:
        await say(ctx, "❌ Ese usuario no está baneado.")
        return
    except discord.Forbidden:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    await say(ctx, f"✅ Usuario `{uid}` desbaneado.")
    add_case(ctx.guild.id, uid, ctx.author.id, "unban", razon)
    await send_log(ctx.guild, "✅ Unban de Usuario", f"🆔 **ID:** `{uid}`\n🛡️ **Moderador:** {ctx.author.mention}\n📝 **Razón:** {razon}", color=discord.Color.green())


@bot.command(name="timeout", aliases=["mute", "silenciar"])
@commands.has_permissions(moderate_members=True)
async def p_timeout(ctx, usuario: discord.Member, minutos: commands.Range[int, 1, 40320], *, razon: str = "No especificada"):
    if not can_moderate(ctx, usuario):
        await say(ctx, pmsg(ctx, "hierarchy"))
        return
    if await timeout_member(usuario, minutos, razon):
        await say(ctx, f"🔇 {usuario.mention} silenciado por **{minutos}** min. Razón: {razon}")
        add_case(ctx.guild.id, usuario.id, ctx.author.id, "timeout", f"{razon} ({minutos} min)")
        await send_log(ctx.guild, "🔇 Timeout", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {ctx.author.mention}\n⏱️ **Duración:** {minutos} min\n📝 **Razón:** {razon}", color=discord.Color.orange())
    else:
        await say(ctx, pmsg(ctx, "bot_no_perm"))


@bot.command(name="untimeout", aliases=["unmute", "desilenciar"])
@commands.has_permissions(moderate_members=True)
async def p_untimeout(ctx, usuario: discord.Member):
    try:
        await usuario.timeout(None, reason=f"Untimeout por {ctx.author}")
    except discord.HTTPException:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    await say(ctx, f"🔊 {usuario.mention} ya puede hablar de nuevo.")
    await send_log(ctx.guild, "🔊 Timeout Removido", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {ctx.author.mention}", color=discord.Color.green())


@bot.command(name="warn", aliases=["advertir"])
@commands.has_permissions(moderate_members=True)
async def p_warn(ctx, usuario: discord.Member, *, razon: str):
    if not can_moderate(ctx, usuario):
        await say(ctx, pmsg(ctx, "hierarchy"))
        return
    total, extra = await apply_warn(ctx.guild, usuario, ctx.author, razon)
    await say(ctx, f"⚠️ {usuario.mention} fue advertido (**{total}** en total).\n📝 Razón: {razon}{extra}")
    add_case(ctx.guild.id, usuario.id, ctx.author.id, "warn", razon)
    await send_log(ctx.guild, "⚠️ Advertencia", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {ctx.author.mention}\n📝 **Razón:** {razon}\n🔢 **Total:** {total}", color=discord.Color.gold())


@bot.command(name="warns", aliases=["advertencias"])
@commands.has_permissions(moderate_members=True)
async def p_warns(ctx, usuario: discord.Member):
    user_warns = DATA["warns"].get(str(ctx.guild.id), {}).get(str(usuario.id), [])
    if not user_warns:
        await say(ctx, f"✅ {usuario.mention} no tiene advertencias.")
        return
    lines = [f"`{i}.` <t:{w['date']}:R> — {w['reason']} (por <@{w['mod']}>)" for i, w in enumerate(user_warns[-10:], start=max(1, len(user_warns) - 9))]
    embed = discord.Embed(title=f"⚠️ Advertencias de {usuario}", description="\n".join(lines), color=discord.Color.gold())
    embed.set_footer(text=f"Total: {len(user_warns)}")
    await ctx.send(embed=embed)


@bot.command(name="clearwarns", aliases=["borrarwarns"])
@commands.has_permissions(moderate_members=True)
async def p_clearwarns(ctx, usuario: discord.Member):
    removed = DATA["warns"].get(str(ctx.guild.id), {}).pop(str(usuario.id), [])
    save_data()
    await say(ctx, f"🧽 Se borraron **{len(removed)}** advertencias de {usuario.mention}.")
    await send_log(ctx.guild, "🧽 Advertencias Borradas", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {ctx.author.mention}\n🔢 **Borradas:** {len(removed)}", color=discord.Color.green())


@bot.command(name="purge", aliases=["clear", "limpiar"])
@commands.has_permissions(manage_messages=True)
async def p_purge(ctx, cantidad: int):
    if cantidad < 1 or cantidad > 100:
        await say(ctx, "❌ **[ERROR]** Solo puedes borrar entre 1 y 100 mensajes a la vez.")
        return
    try:
        deleted = await ctx.channel.purge(limit=cantidad + 1)  # +1 = el propio comando
    except discord.Forbidden:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    count = max(len(deleted) - 1, 0)
    await say(ctx, f"🧹 Se han eliminado **{count}** mensajes correctamente.", delete_after=5)
    await send_log(ctx.guild, "🧹 Purge de Mensajes", f"🛡️ **Moderador:** {ctx.author.mention}\n💬 **Canal:** {ctx.channel.mention}\n🔢 **Cantidad:** `{count}` mensajes.", color=discord.Color.purple())


@bot.command(name="slowmode", aliases=["modolento"])
@commands.has_permissions(manage_channels=True)
async def p_slowmode(ctx, segundos: commands.Range[int, 0, 21600]):
    if not isinstance(ctx.channel, discord.TextChannel):
        await say(ctx, pmsg(ctx, "not_text"))
        return
    try:
        await ctx.channel.edit(slowmode_delay=segundos, reason=f"Slowmode por {ctx.author}")
    except discord.Forbidden:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    await say(ctx, f"🐢 Modo lento: **{segundos}s**." if segundos else "🐇 Modo lento desactivado.")


# --- Protección ---
@bot.command(name="lock", aliases=["bloquear"])
@commands.has_permissions(manage_channels=True)
async def p_lock(ctx, *, razon: str = "No especificada"):
    channel = ctx.channel
    if not isinstance(channel, discord.TextChannel):
        await say(ctx, pmsg(ctx, "not_text"))
        return
    role = ctx.guild.default_role
    overwrite = channel.overwrites_for(role)
    overwrite.send_messages = False
    try:
        await channel.set_permissions(role, overwrite=overwrite, reason=f"Lock: {razon}")
    except discord.Forbidden:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    await say(ctx, f"🔒 Canal bloqueado. Razón: {razon}")
    await send_log(ctx.guild, "🔒 Canal Bloqueado", f"💬 **Canal:** {channel.mention}\n🛡️ **Moderador:** {ctx.author.mention}\n📝 **Razón:** {razon}", color=discord.Color.orange())


@bot.command(name="unlock", aliases=["desbloquear"])
@commands.has_permissions(manage_channels=True)
async def p_unlock(ctx):
    channel = ctx.channel
    if not isinstance(channel, discord.TextChannel):
        await say(ctx, pmsg(ctx, "not_text"))
        return
    role = ctx.guild.default_role
    overwrite = channel.overwrites_for(role)
    overwrite.send_messages = None
    try:
        await channel.set_permissions(role, overwrite=None if overwrite.is_empty() else overwrite, reason=f"Unlock por {ctx.author}")
    except discord.Forbidden:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    await say(ctx, "🔓 Canal desbloqueado.")
    await send_log(ctx.guild, "🔓 Canal Desbloqueado", f"💬 **Canal:** {channel.mention}\n🛡️ **Moderador:** {ctx.author.mention}", color=discord.Color.green())


@bot.command(name="lockdown")
@commands.has_permissions(administrator=True)
async def p_lockdown(ctx, *, razon: str = "Emergencia"):
    async with ctx.typing():
        count = await do_lockdown(ctx.guild, razon)
    await say(ctx, f"🚨 Lockdown activo: **{count}** canales bloqueados. Usa `N!unlockdown` para restaurarlos.")
    await send_log(ctx.guild, "🚨 LOCKDOWN Activado", f"🛡️ **Admin:** {ctx.author.mention}\n📝 **Razón:** {razon}\n🔒 **Canales bloqueados:** {count}", color=discord.Color.dark_red())


@bot.command(name="unlockdown")
@commands.has_permissions(administrator=True)
async def p_unlockdown(ctx):
    async with ctx.typing():
        count = await do_unlockdown(ctx.guild)
    await say(ctx, f"✅ Lockdown finalizado: **{count}** canales restaurados.")
    await send_log(ctx.guild, "✅ Lockdown Finalizado", f"🛡️ **Admin:** {ctx.author.mention}\n🔓 **Canales restaurados:** {count}", color=discord.Color.green())


@bot.command(name="raid-mode", aliases=["raidmode", "raid"])
@commands.has_permissions(administrator=True)
async def p_raid_mode(ctx, activar: bool):
    if activar:
        RAID_MODE_UNTIL[ctx.guild.id] = time.time() + 900
        msg = f"🚨 Modo raid **activado** por 15 minutos: se expulsa a cuentas con menos de {RAID_MIN_ACCOUNT_AGE_DAYS} días."
    else:
        RAID_MODE_UNTIL.pop(ctx.guild.id, None)
        msg = "✅ Modo raid **desactivado**."
    await say(ctx, msg)
    await send_log(ctx.guild, "🚨 Modo Raid (manual)", f"🛡️ **Admin:** {ctx.author.mention}\n{msg}", color=discord.Color.orange())


@bot.command(name="config", aliases=["config-proteccion", "configproteccion", "proteccion"])
@commands.has_permissions(administrator=True)
async def p_config(ctx, modulo: str, estado: bool):
    key = modulo.lower()
    if key not in MODULES:
        await say(ctx, pmsg(ctx, "bad_module", options=", ".join(f"`{k}`" for k in MODULES)))
        return
    DATA["config"].setdefault(str(ctx.guild.id), {})[key] = estado
    save_data()
    await say(ctx, f"{'🟢' if estado else '🔴'} **{MODULES[key]}** ahora está **{'activado' if estado else 'desactivado'}**.")
    await send_log(ctx.guild, "⚙️ Configuración de Protección", f"🛡️ **Admin:** {ctx.author.mention}\n📦 **Módulo:** {MODULES[key]}\n📌 **Estado:** {'Activado' if estado else 'Desactivado'}", color=discord.Color.blue())


@bot.group(name="whitelist", aliases=["wl"], invoke_without_command=True)
async def p_whitelist(ctx):
    await say(ctx, "Uso: `N!whitelist add @usuario` • `N!whitelist remove @usuario` • `N!whitelist list`")


@p_whitelist.command(name="add")
@p_guild_owner_only()
async def p_wl_add(ctx, usuario: discord.User):
    wl = DATA["whitelist"].setdefault(str(ctx.guild.id), [])
    if usuario.id not in wl:
        wl.append(usuario.id)
        save_data()
    await say(ctx, f"✅ {usuario.mention} añadido a la whitelist.")
    await send_log(ctx.guild, "✅ Whitelist • Añadido", f"👤 {usuario.mention} (`{usuario.id}`)\n🛡️ **Por:** {ctx.author.mention}", color=discord.Color.green())


@p_whitelist.command(name="remove", aliases=["del"])
@p_guild_owner_only()
async def p_wl_remove(ctx, usuario: discord.User):
    wl = DATA["whitelist"].setdefault(str(ctx.guild.id), [])
    if usuario.id in wl:
        wl.remove(usuario.id)
        save_data()
    await say(ctx, f"🗑️ {usuario.mention} quitado de la whitelist.")
    await send_log(ctx.guild, "🗑️ Whitelist • Quitado", f"👤 {usuario.mention} (`{usuario.id}`)\n🛡️ **Por:** {ctx.author.mention}", color=discord.Color.orange())


@p_whitelist.command(name="list")
@commands.has_permissions(administrator=True)
async def p_wl_list(ctx):
    wl = DATA["whitelist"].get(str(ctx.guild.id), [])
    text = "\n".join(f"• <@{uid}> (`{uid}`)" for uid in wl) or "Nadie (aparte del dueño del servidor)."
    await say(ctx, embed=discord.Embed(title="✅ Whitelist", description=text, color=discord.Color.green()))


# --- Bans globales (solo dueño del bot) ---
@bot.command(name="gban", aliases=["globalban"])
@p_bot_owner_only()
async def p_gban(ctx, usuario_id: str, *, razon: str = "No especificada"):
    uid = parse_user_id(usuario_id)
    if uid is None:
        await say(ctx, pmsg(ctx, "bad_id"))
        return
    if uid in (bot.user.id, ctx.author.id) or uid in OWNER_IDS:
        await say(ctx, pmsg(ctx, "hierarchy"))
        return
    async with ctx.typing():
        ok, total = await do_gban(uid, razon, ctx.author.id)
    await say(ctx, f"🌍 `{uid}` añadido a la lista global y baneado en **{ok}/{total}** servidores. Si entra a otro servidor con Nexus, será baneado automáticamente.")


@bot.command(name="ungban", aliases=["globalunban"])
@p_bot_owner_only()
async def p_ungban(ctx, usuario_id: str):
    uid = parse_user_id(usuario_id)
    if uid is None:
        await say(ctx, pmsg(ctx, "bad_id"))
        return
    async with ctx.typing():
        existed, unbanned = await do_ungban(uid)
    await say(ctx, f"{'✅' if existed else 'ℹ️'} `{uid}` {'retirado de la lista global' if existed else 'no estaba en la lista global'}. Desbaneado en **{unbanned}** servidores.")


@bot.command(name="gban-list", aliases=["gbanlist", "gbans"])
@p_bot_owner_only()
async def p_gban_list(ctx):
    if not DATA["gbans"]:
        await say(ctx, "✅ La lista de bans globales está vacía.")
        return
    lines = [f"`{uid}` — {info.get('reason', 'Sin razón')}" for uid, info in list(DATA["gbans"].items())[:25]]
    embed = discord.Embed(title="🌍 Bans Globales", description="\n".join(lines), color=discord.Color.dark_red())
    embed.set_footer(text=f"Total: {len(DATA['gbans'])}")
    try:
        await ctx.author.send(embed=embed)
        await say(ctx, "📩 Te envié la lista por MD.")
    except discord.HTTPException:
        await say(ctx, embed=embed, delete_after=30)


# --- Setup / Backup ---
@bot.command(name="setup-onboarding", aliases=["setuponboarding"])
@commands.has_permissions(administrator=True)
async def p_setup_onboarding(ctx):
    embed = discord.Embed(
        title="🌐 Selecciona tu Idioma / Select Your Language",
        description="🇪🇸 Haz clic en tu idioma para personalizar los canales y el sistema de tickets.\n\n🇺🇸 Click on your language button below to customize channels and ticket support.",
        color=discord.Color.blue()
    )
    await ctx.channel.send(embed=embed, view=LanguageRoleView())


@bot.command(name="setup-panel-tickets", aliases=["setuppaneltickets", "setup-tickets"])
@commands.has_permissions(administrator=True)
async def p_setup_panel_tickets(ctx):
    embed = discord.Embed(
        title="🎟️ Panel de Soporte y Apelaciones / Support Panel",
        description="👇 Presiona el botón de abajo para abrir un ticket de soporte privado.\n\nPress the button below to open a private support ticket.",
        color=discord.Color.blue()
    )
    embed.set_footer(text="Nexus Protect • Support Engine")
    await ctx.channel.send(embed=embed, view=TicketView())


@bot.command(name="setup-antiraid", aliases=["setupantiraid"])
@commands.has_permissions(administrator=True)
async def p_setup_antiraid(ctx):
    await send_log(ctx.guild, "🛡️ Sistema de Auditoría & AntiRaid Activado", "✅ El canal de registros e historial de seguridad está 100% operativo.", color=discord.Color.green())
    await say(ctx, "✅ **[OK]** Canal `#nexus-logs` configurado.")


@bot.command(name="backup-create", aliases=["backupcreate", "backup"])
@commands.has_permissions(administrator=True)
async def p_backup_create(ctx):
    guild = ctx.guild
    async with ctx.typing():
        backup_data = build_backup_data(guild)
        json_bytes = json.dumps(backup_data, indent=4).encode("utf-8")
        filename = f"backup-{guild.name.replace(' ', '_')}.json"
        try:
            embed_dm = discord.Embed(
                title="📦 Copia de Seguridad Generada",
                description=f"🏰 **Servidor:** {guild.name}\n"
                            f"🎭 **Roles guardados:** `{len(backup_data['roles'])}`\n"
                            f"📂 **Categorías:** `{len(backup_data['categories'])}`",
                color=discord.Color.green()
            )
            await ctx.author.send(embed=embed_dm, file=discord.File(io.BytesIO(json_bytes), filename=filename))
            await say(ctx, "✅ Backup creado con éxito. Te enviamos el archivo `.json` por **Mensaje Directo**.")
        except discord.HTTPException:
            await say(ctx, "⚠️ Se creó el backup, pero no pudimos enviártelo por MD. Se guardó en `#nexus-logs`.")
        await send_log(
            guild,
            "📦 Backup Creado",
            f"👤 **Administrador:** {ctx.author.mention}\n💾 El respaldo de la estructura fue generado exitosamente.",
            color=discord.Color.gold(),
            file=discord.File(io.BytesIO(json_bytes), filename=filename)
        )


@bot.command(name="backup-load", aliases=["backupload"])
@commands.has_permissions(administrator=True)
async def p_backup_load(ctx):
    if not ctx.message.attachments or not ctx.message.attachments[0].filename.endswith(".json"):
        await say(ctx, "❌ Adjunta un archivo `.json` válido junto al comando: `N!backup-load` + archivo.")
        return
    archivo = ctx.message.attachments[0]
    async with ctx.typing():
        try:
            content = await archivo.read()
            data = json.loads(content.decode("utf-8"))
            await restore_backup(ctx.guild, data)
        except Exception as e:
            await say(ctx, f"❌ Error al procesar el archivo de backup: `{e}`")
            return
    await say(ctx, "✅ Estructura del backup restaurada/cargada con éxito.")
    await send_log(ctx.guild, "📥 Backup Cargado", f"👤 **Administrador:** {ctx.author.mention}\nSe aplicó el archivo `{archivo.filename}`.", color=discord.Color.blue())


# =====================================================================
# EXTRAS: ESTILO, AYUDA INTERACTIVA, ANTINUKE+, BIENVENIDA, VERIFICACIÓN
# =====================================================================
COLORS = {"ok": 0x57F287, "err": 0xED4245, "warn": 0xFEE75C, "info": 0x5865F2, "brand": 0x2B6CFF}

SNIPE_CACHE = {}
PROCESSED_ENTRIES = set()
REMINDERS = {}
REMINDER_TASKS = set()
CRITICAL_PERMS_MASK = discord.Permissions(
    administrator=True, manage_guild=True, manage_roles=True, manage_channels=True,
    ban_members=True, kick_members=True, manage_webhooks=True
).value


def color_for_text(text):
    t = (text or "").lstrip()
    if t.startswith(("✅", "🟢", "🔊", "🔓", "🐇", "📩", "🧽", "🏓")):
        return COLORS["ok"]
    if t.startswith(("❌", "⛔", "🚫", "🚨")):
        return COLORS["err"]
    if t.startswith(("⚠️", "🔇", "🔒", "👢", "🐢")):
        return COLORS["warn"]
    return COLORS["info"]


def styled(title=None, description=None, kind="info", footer="Nexus Protect"):
    embed = discord.Embed(
        title=title,
        description=description,
        color=COLORS.get(kind, COLORS["info"]),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=footer)
    return embed


def apply_setting(guild, key, value):
    st = DATA["settings"].setdefault(str(guild.id), {})
    if value is None:
        st.pop(key, None)
    else:
        st[key] = value
    save_data()


def has_admin_perms(role):
    p = role.permissions
    return p.administrator or p.manage_guild or p.manage_roles


def validate_safe_role(rol):
    if rol.is_default() or rol.managed:
        return "Ese rol no se puede usar (es @everyone o lo gestiona una integración)."
    if has_dangerous_perms(rol):
        return "Por seguridad no se permiten roles con permisos peligrosos (admin, ban, kick, gestionar roles/canales/webhooks)."
    if rol >= rol.guild.me.top_role:
        return "Ese rol está por encima o al mismo nivel que el mío. Súbeme de jerarquía para poder asignarlo."
    return None


# =====================================================================
# ANTINUKE+ (webhooks, escalada de permisos, renombrar servidor)
# =====================================================================
async def check_dangerous_role_grant(before, after):
    guild = after.guild
    if not module_enabled(guild.id, "antiroles"):
        return
    new_roles = [r for r in after.roles if r not in before.roles and not r.managed and has_admin_perms(r)]
    if not new_roles:
        return
    executor = await get_executor(guild, discord.AuditLogAction.member_role_update, after.id)
    if executor is None or is_trusted(guild, executor):
        return
    try:
        await after.remove_roles(*new_roles, reason="Nexus AntiRoles: rol peligroso asignado sin autorización")
        result = "↩️ El/los rol(es) fueron **quitados**."
    except discord.HTTPException:
        result = "⚠️ No pude quitarlos (jerarquía de roles). Revísalo manualmente."
    await send_log(
        guild,
        "🚨 ANTIROLES • Rol Peligroso Asignado",
        f"👤 **Responsable:** {executor.mention} (`{executor.id}`)\n"
        f"🎯 **Receptor:** {after.mention}\n"
        f"🎭 **Rol(es):** {', '.join(r.mention for r in new_roles)}\n{result}",
        color=discord.Color.dark_red()
    )
    await antinuke_check(guild, "role_grant", executor, "Asignación de Roles de Administrador")


@bot.event
async def on_guild_role_update(before, after):
    guild = after.guild
    if not module_enabled(guild.id, "antiroles"):
        return
    gained = after.permissions.value & ~before.permissions.value & CRITICAL_PERMS_MASK
    if not gained:
        return
    executor = await get_executor(guild, discord.AuditLogAction.role_update, after.id)
    if executor is None or is_trusted(guild, executor):
        return
    try:
        await after.edit(permissions=before.permissions, reason="Nexus AntiRoles: escalada de permisos revertida")
        result = "↩️ Los permisos fueron **revertidos**."
    except discord.HTTPException:
        result = "⚠️ No pude revertirlos (jerarquía de roles). Revísalo manualmente."
    await send_log(
        guild,
        "🚨 ANTIROLES • Escalada de Permisos",
        f"👤 **Responsable:** {executor.mention} (`{executor.id}`)\n🎭 **Rol:** {after.mention}\n{result}",
        color=discord.Color.dark_red()
    )
    await antinuke_check(guild, "role_perm_escalation", executor, "Escalada de Permisos en Roles")


@bot.event
async def on_webhooks_update(channel):
    guild = channel.guild
    if not module_enabled(guild.id, "antinuke"):
        return
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.webhook_create):
            if (discord.utils.utcnow() - entry.created_at).total_seconds() > 10:
                continue
            if entry.id in PROCESSED_ENTRIES:
                continue
            PROCESSED_ENTRIES.add(entry.id)
            if len(PROCESSED_ENTRIES) > 500:
                PROCESSED_ENTRIES.clear()

            executor = entry.user
            if executor is None or is_trusted(guild, executor):
                continue

            deleted = False
            try:
                for wh in await channel.webhooks():
                    if wh.id == getattr(entry.target, "id", None):
                        await wh.delete(reason="Nexus AntiNuke: webhook no autorizado")
                        deleted = True
            except discord.HTTPException:
                pass

            await send_log(
                guild,
                "🚨 ANTINUKE • Webhook No Autorizado",
                f"👤 **Creado por:** {executor.mention} (`{executor.id}`)\n💬 **Canal:** {channel.mention}\n"
                f"{'🗑️ El webhook fue **eliminado**.' if deleted else '⚠️ No pude eliminarlo, revísalo manualmente.'}",
                color=discord.Color.dark_red()
            )
            await antinuke_check(guild, "webhook_create", executor, "Creación Masiva de Webhooks")
    except (discord.Forbidden, discord.HTTPException):
        return


@bot.event
async def on_guild_update(before, after):
    if before.name == after.name or not module_enabled(after.id, "antinuke"):
        return
    executor = await get_executor(after, discord.AuditLogAction.guild_update)
    if executor is None or is_trusted(after, executor):
        return
    try:
        await after.edit(name=before.name, reason="Nexus AntiNuke: cambio de nombre revertido")
        result = "↩️ El nombre fue **restaurado**."
    except discord.HTTPException:
        result = "⚠️ No pude restaurar el nombre."
    await send_log(
        after,
        "🚨 ANTINUKE • Nombre del Servidor Cambiado",
        f"👤 **Responsable:** {executor.mention} (`{executor.id}`)\n"
        f"📝 **Antes:** `{before.name}`\n📌 **Después:** `{after.name}`\n{result}",
        color=discord.Color.dark_red()
    )


# =====================================================================
# BIENVENIDA + AUTOROLE
# =====================================================================
def build_welcome_embed(member):
    guild = member.guild
    embed = discord.Embed(
        title=f"✨ ¡Bienvenido/a a {guild.name}!",
        description=f"Hola {member.mention}, nos alegra tenerte aquí. 🎉\n\n"
                    f"👥 Eres el miembro **#{guild.member_count}**\n"
                    f"📅 Cuenta creada <t:{int(member.created_at.timestamp())}:R>",
        color=COLORS["brand"],
        timestamp=discord.utils.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    if guild.icon:
        embed.set_author(name=guild.name, icon_url=guild.icon.url)
    embed.set_footer(text="Nexus Protect • Welcome")
    return embed


async def handle_welcome(member):
    guild = member.guild
    st = DATA["settings"].get(str(guild.id), {})

    role_id = st.get("autorole")
    if role_id:
        role = guild.get_role(int(role_id))
        if role:
            try:
                await member.add_roles(role, reason="Nexus AutoRole")
            except discord.HTTPException:
                pass

    channel_id = st.get("welcome_channel")
    if channel_id:
        channel = guild.get_channel(int(channel_id))
        if channel:
            try:
                await channel.send(
                    content=member.mention,
                    embed=build_welcome_embed(member),
                    allowed_mentions=discord.AllowedMentions(users=True)
                )
            except discord.HTTPException:
                pass


def do_setwelcome(guild, canal):
    apply_setting(guild, "welcome_channel", canal.id if canal else None)
    if canal:
        return styled("✅ Bienvenida configurada", f"Los mensajes de bienvenida se enviarán en {canal.mention}.", "ok")
    return styled("🔴 Bienvenida desactivada", "Ya no se enviarán mensajes de bienvenida.", "warn")


def do_setlogs(guild, canal):
    apply_setting(guild, "log_channel", canal.id if canal else None)
    if canal:
        return styled("✅ Canal de logs configurado", f"Los registros de seguridad se enviarán en {canal.mention}.", "ok")
    return styled("🔴 Canal de logs restablecido", "Se usará de nuevo el canal `#nexus-logs`.", "warn")


def do_autorole(guild, rol):
    if rol is None:
        apply_setting(guild, "autorole", None)
        return styled("🔴 AutoRole desactivado", "Los nuevos miembros ya no recibirán un rol automático.", "warn")
    error = validate_safe_role(rol)
    if error:
        return styled("❌ Error", error, "err")
    apply_setting(guild, "autorole", rol.id)
    return styled("✅ AutoRole configurado", f"Los nuevos miembros recibirán {rol.mention}.", "ok")


@bot.tree.command(name="setwelcome", description="Configura el canal de bienvenida (sin canal = desactivar).")
@app_commands.describe(canal="Canal donde se enviarán las bienvenidas")
@app_commands.checks.has_permissions(manage_guild=True)
async def setwelcome(interaction: discord.Interaction, canal: discord.TextChannel = None):
    await interaction.response.send_message(embed=do_setwelcome(interaction.guild, canal), ephemeral=True)


@bot.command(name="setwelcome", aliases=["bienvenida"])
@commands.has_permissions(manage_guild=True)
async def p_setwelcome(ctx, canal: discord.TextChannel = None):
    await say(ctx, embed=do_setwelcome(ctx.guild, canal))


@bot.tree.command(name="setlogs", description="Elige el canal de logs de seguridad (sin canal = restablecer).")
@app_commands.describe(canal="Canal para los logs")
@app_commands.checks.has_permissions(manage_guild=True)
async def setlogs(interaction: discord.Interaction, canal: discord.TextChannel = None):
    await interaction.response.send_message(embed=do_setlogs(interaction.guild, canal), ephemeral=True)


@bot.command(name="setlogs", aliases=["logs"])
@commands.has_permissions(manage_guild=True)
async def p_setlogs(ctx, canal: discord.TextChannel = None):
    await say(ctx, embed=do_setlogs(ctx.guild, canal))


@bot.tree.command(name="autorole", description="Rol que se da automáticamente a los nuevos miembros (sin rol = desactivar).")
@app_commands.describe(rol="Rol a asignar")
@app_commands.checks.has_permissions(manage_guild=True)
async def autorole(interaction: discord.Interaction, rol: discord.Role = None):
    await interaction.response.send_message(embed=do_autorole(interaction.guild, rol), ephemeral=True)


@bot.command(name="autorole", aliases=["autorol"])
@commands.has_permissions(manage_guild=True)
async def p_autorole(ctx, rol: discord.Role = None):
    await say(ctx, embed=do_autorole(ctx.guild, rol))


# =====================================================================
# VERIFICACIÓN POR BOTÓN
# =====================================================================
VERIFY_TEXTS = {
    "es": {
        "no_role": "❌ **[ERROR]** El rol de verificación no está configurado.",
        "already": "✅ Ya estás verificado.",
        "done": "✅ ¡Verificado! Ya tienes acceso al servidor.",
        "fail": "❌ **[ERROR]** No pude darte el rol. Avisa a un administrador.",
    },
    "en": {
        "no_role": "❌ **[ERROR]** The verification role is not configured.",
        "already": "✅ You are already verified.",
        "done": "✅ Verified! You now have access to the server.",
        "fail": "❌ **[ERROR]** I couldn't give you the role. Please tell an administrator.",
    }
}


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Verificarme / Verify", style=discord.ButtonStyle.success, custom_id="verify_btn")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        txt = VERIFY_TEXTS[get_lang(interaction.user, interaction.guild, interaction.locale)]
        role_id = DATA["settings"].get(str(interaction.guild.id), {}).get("verify_role")
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        if not role:
            await interaction.response.send_message(txt["no_role"], ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message(txt["already"], ephemeral=True)
            return
        if DATA["settings"].get(str(interaction.guild.id), {}).get("verify_captcha"):
            await interaction.response.send_modal(start_captcha(interaction, role))
            return
        await give_verify_role(interaction, role, txt)


async def do_setup_verify(channel, guild, rol, captcha=False):
    error = validate_safe_role(rol)
    if error:
        return styled("❌ Error", error, "err")
    apply_setting(guild, "verify_role", rol.id)
    apply_setting(guild, "verify_captcha", True if captcha else None)
    panel = styled(
        "✅ Verificación / Verification",
        "🇪🇸 Presiona el botón de abajo para verificarte y acceder al servidor.\n\n"
        "🇺🇸 Press the button below to verify yourself and get access to the server.",
        "ok", "Nexus Protect • Verification"
    )
    try:
        await channel.send(embed=panel, view=VerifyView())
    except discord.Forbidden:
        return styled("❌ Error", "No tengo permisos para escribir en este canal.", "err")
    return styled("✅ Panel de verificación creado", f"Rol de verificación: {rol.mention}" + ("\n🔐 Con código de verificación." if captcha else ""), "ok")


@bot.tree.command(name="setup-verify", description="Crea un panel de verificación con botón que da un rol.")
@app_commands.describe(rol="Rol que se entrega al verificarse", captcha="Pedir un código al verificarse")
@app_commands.checks.has_permissions(administrator=True)
async def setup_verify(interaction: discord.Interaction, rol: discord.Role, captcha: bool = False):
    await interaction.response.defer(ephemeral=True)
    result = await do_setup_verify(interaction.channel, interaction.guild, rol, captcha)
    await interaction.followup.send(embed=result, ephemeral=True)


@bot.command(name="setup-verify", aliases=["setupverify"])
@commands.has_permissions(administrator=True)
async def p_setup_verify(ctx, rol: discord.Role, captcha: bool = False):
    result = await do_setup_verify(ctx.channel, ctx.guild, rol, captcha)
    await say(ctx, embed=result)


# =====================================================================
# UTILIDADES (avatar, poll, announce, snipe, remind, ping)
# =====================================================================
POLL_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def parse_duration(text):
    text = (text or "").lower().replace(" ", "")
    parts = re.findall(r"(\d+)([smhd])", text)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != text:
        return None
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return sum(int(n) * mult[u] for n, u in parts)


def build_avatar_embed(user):
    embed = styled(f"🖼️ Avatar de {user.display_name}", f"[Abrir imagen original]({user.display_avatar.url})", "brand")
    embed.set_image(url=user.display_avatar.url)
    return embed


async def post_poll(channel, author, pregunta, opciones):
    opciones = [o.strip() for o in opciones if o.strip()]
    if not pregunta.strip() or len(opciones) < 2 or len(opciones) > 9:
        return False
    description = "\n".join(f"{POLL_EMOJIS[i]}  {o[:200]}" for i, o in enumerate(opciones))
    embed = styled(f"📊 {pregunta.strip()[:240]}", description, "brand", f"Encuesta de {author.display_name}")
    message = await channel.send(embed=embed)
    for i in range(len(opciones)):
        await message.add_reaction(POLL_EMOJIS[i])
    return True


def build_announcement(author, titulo, mensaje):
    return styled(f"📢 {titulo.strip()[:240]}", mensaje.replace("\\n", "\n")[:3900], "brand", f"Anuncio por {author.display_name}")


def build_snipe_embed(channel_id):
    data = SNIPE_CACHE.get(channel_id)
    if not data:
        return styled("🕵️ Snipe", "No hay mensajes eliminados recientes en este canal.", "warn")
    embed = styled("🕵️ Último mensaje eliminado", data["content"], "warn", "Nexus Protect • Snipe")
    embed.set_author(name=data["author"], icon_url=data["avatar"])
    embed.add_field(name="Eliminado", value=f"<t:{data['ts']}:R>")
    return embed


async def reminder_task(user_id, channel, seconds, texto):
    try:
        await asyncio.sleep(seconds)
        embed = styled("⏰ Recordatorio", texto, "info")
        try:
            await channel.send(f"<@{user_id}>", embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
        except discord.HTTPException:
            user = bot.get_user(user_id)
            if user:
                try:
                    await user.send(embed=embed)
                except discord.HTTPException:
                    pass
    finally:
        REMINDERS[user_id] = max(REMINDERS.get(user_id, 1) - 1, 0)


def schedule_reminder(user, channel, tiempo, texto):
    seconds = parse_duration(tiempo)
    if seconds is None or seconds < 5 or seconds > 86400:
        return styled("❌ Tiempo inválido", "Usa un tiempo entre `5s` y `24h`. Ejemplos: `30m`, `2h`, `1h30m`.", "err")
    if REMINDERS.get(user.id, 0) >= 5:
        return styled("❌ Límite alcanzado", "Solo puedes tener 5 recordatorios activos a la vez.", "err")
    REMINDERS[user.id] = REMINDERS.get(user.id, 0) + 1
    task = asyncio.create_task(reminder_task(user.id, channel, seconds, texto[:500]))
    REMINDER_TASKS.add(task)
    task.add_done_callback(REMINDER_TASKS.discard)
    when = int(time.time()) + seconds
    return styled("⏰ Recordatorio creado", f"Te avisaré <t:{when}:R>.\n📝 {texto[:500]}\n\n*Si el bot se reinicia antes, se pierde.*", "ok")


@bot.tree.command(name="ping", description="Muestra la latencia del bot.")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message(embed=styled("🏓 Pong!", f"Latencia: **{round(bot.latency * 1000)}ms**", "ok"), ephemeral=True)


@bot.tree.command(name="avatar", description="Muestra el avatar de un usuario.")
@app_commands.describe(usuario="Usuario (por defecto, tú)")
async def avatar_slash(interaction: discord.Interaction, usuario: discord.Member = None):
    await interaction.response.send_message(embed=build_avatar_embed(usuario or interaction.user))


@bot.command(name="avatar", aliases=["av", "pfp"])
async def p_avatar(ctx, usuario: discord.Member = None):
    await ctx.send(embed=build_avatar_embed(usuario or ctx.author))


@bot.tree.command(name="poll", description="Crea una encuesta con reacciones (2 a 9 opciones).")
@app_commands.describe(pregunta="La pregunta", opciones="Opciones separadas por | (ej: Sí | No | Tal vez)")
async def poll_slash(interaction: discord.Interaction, pregunta: str, opciones: str):
    await interaction.response.defer(ephemeral=True)
    try:
        ok = await post_poll(interaction.channel, interaction.user, pregunta, opciones.split("|"))
    except discord.HTTPException:
        ok = False
    await interaction.followup.send("✅ Encuesta creada." if ok else "❌ Pon entre 2 y 9 opciones separadas por `|`.", ephemeral=True)


@bot.command(name="poll", aliases=["encuesta"])
async def p_poll(ctx, *, texto: str):
    partes = [p.strip() for p in texto.split("|")]
    try:
        ok = await post_poll(ctx.channel, ctx.author, partes[0], partes[1:])
    except discord.HTTPException:
        ok = False
    if ok:
        await delete_quietly(ctx.message)
    else:
        await say(ctx, "❌ Uso: `N!poll ¿Pregunta? | opción 1 | opción 2` (2 a 9 opciones).")


@bot.tree.command(name="announce", description="Publica un anuncio con embed en un canal.")
@app_commands.describe(canal="Canal destino", titulo="Título del anuncio", mensaje="Texto (usa \\n para saltos de línea)")
@app_commands.checks.has_permissions(manage_messages=True)
async def announce_slash(interaction: discord.Interaction, canal: discord.TextChannel, titulo: str, mensaje: str):
    try:
        await canal.send(embed=build_announcement(interaction.user, titulo, mensaje))
    except discord.Forbidden:
        await interaction.response.send_message(tr(interaction, "bot_no_perm"), ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Anuncio publicado en {canal.mention}.", ephemeral=True)


@bot.command(name="announce", aliases=["anuncio"])
@commands.has_permissions(manage_messages=True)
async def p_announce(ctx, canal: discord.TextChannel, *, texto: str):
    if "|" not in texto:
        await say(ctx, "❌ Uso: `N!announce #canal Título | Mensaje`")
        return
    titulo, mensaje = [p.strip() for p in texto.split("|", 1)]
    try:
        await canal.send(embed=build_announcement(ctx.author, titulo, mensaje))
    except discord.Forbidden:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    await say(ctx, f"✅ Anuncio publicado en {canal.mention}.")


@bot.tree.command(name="snipe", description="Muestra el último mensaje eliminado en este canal.")
@app_commands.checks.has_permissions(manage_messages=True)
async def snipe_slash(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_snipe_embed(interaction.channel.id), ephemeral=True)


@bot.command(name="snipe")
@commands.has_permissions(manage_messages=True)
async def p_snipe(ctx):
    await say(ctx, embed=build_snipe_embed(ctx.channel.id))


@bot.tree.command(name="remind", description="Crea un recordatorio (de 5s a 24h).")
@app_commands.describe(tiempo="Ej: 30m, 2h, 1h30m", texto="Qué quieres que te recuerde")
async def remind_slash(interaction: discord.Interaction, tiempo: str, texto: str):
    await interaction.response.send_message(embed=schedule_reminder(interaction.user, interaction.channel, tiempo, texto), ephemeral=True)


@bot.command(name="remind", aliases=["recordar", "reminder"])
async def p_remind(ctx, tiempo: str, *, texto: str):
    await say(ctx, embed=schedule_reminder(ctx.author, ctx.channel, tiempo, texto))


# =====================================================================
# AYUDA INTERACTIVA (menú desplegable)
# =====================================================================
HELP_PAGES = {
    "mod": ("⚔️", "Moderación", "Sanciones y control de usuarios",
            "`kick` • `ban` • `unban` • `timeout` • `untimeout` • `warn` • `warns` • `clearwarns` • `modlogs` • `purge` • `slowmode`\n\n"
            "💡 Advertencias automáticas: **3** = 1 h de timeout, **5** = expulsión, **7** = baneo."),
    "prot": ("🛡️", "Protección", "Bloqueos, raid y módulos",
             "`lock` • `unlock` • `lockdown` • `unlockdown` • `panic` • `raid-mode` • `config` • `minage` • `wordfilter add|remove|list` • `whitelist add|remove|list`\n\n"
             "**Módulos (`config <módulo> on|off`):**\n" + "\n".join(f"• `{k}` — {v}" for k, v in MODULES.items())),
    "nuke": ("🚨", "AntiNuke (automático)", "Lo que vigila el bot por ti",
             "🔹 Creación/eliminación masiva de canales y roles\n"
             "🔹 Baneos y expulsiones masivas\n"
             "🔹 Bots no autorizados → se expulsan\n"
             "🔹 Webhooks no autorizados → se eliminan\n"
             "🔹 Escalada de permisos en roles → se revierte\n"
             "🔹 Roles de admin dados sin permiso → se quitan\n"
             "🔹 Cambio de nombre del servidor → se revierte\n\n"
             "⚖️ Al superar 3 acciones en 10 s, el responsable es baneado.\n"
             "✅ Los usuarios en la `whitelist` y el dueño quedan exentos."),
    "owner": ("🌍", "Dueño del bot", "Bans globales entre servidores",
              "`gban` • `ungban` • `gban-list`\n\nSolo el dueño del bot puede usarlos."),
    "sys": ("⚙️", "Sistema", "Paneles y configuración",
            "`setup-onboarding` • `setup-panel-tickets` • `setup-antiraid` • `setup-verify` (con `True` pide código)\n"
            "`setwelcome` • `setgoodbye` • `autorole` • `setlogs` • `setsuggest`\n"
            "`setup-stats` • `remove-stats` • `setup-tempvoice` • `rolepanel`\n"
            "`backup-create` • `backup-load`"),
    "comm": ("🎉", "Comunidad", "Sorteos, niveles, traductor y más",
             "`translate` • `giveaway` • `gend` • `greroll` • `rank` • `leaderboard` • `levelrole` • `suggest` • `suggestion` • `rolepanel`\n\n"
             "🌐 `N!translate en hola mundo`, o responde a un mensaje con `N!translate`. También: clic derecho en un mensaje → Apps → Traducir.\n"
             "📈 Los niveles vienen apagados: actívalos con `N!config levels on`.\n"
             "🎭 `N!rolepanel \"Título\" @rol1 @rol2` crea botones para que cada quien se dé sus roles."),
    "util": ("✨", "Utilidades", "Comodidades del día a día",
             "`userinfo` • `serverinfo` • `serverstats` • `avatar` • `poll` • `announce` • `snipe` • `remind` • `status` • `ping` • `help`"),
}


def help_embed(key="home"):
    mention = bot.user.mention if bot.user else "@Nexus Protect"
    if key == "home" or key not in HELP_PAGES:
        embed = styled(
            "🛡️ Nexus Protect • Centro de Ayuda",
            "Protección total para tu servidor: **AntiNuke**, **AntiRaid**, **Anti Ping**, bans globales, tickets, backups y más.\n\n"
            f"📌 **Prefijos:** `N!comando` • {mention} `comando` • `/comando`\n"
            "👇 Elige una categoría en el menú de abajo.",
            "brand"
        )
        for emoji, label, desc, _ in HELP_PAGES.values():
            embed.add_field(name=f"{emoji} {label}", value=desc, inline=True)
        return embed
    emoji, label, _, text = HELP_PAGES[key]
    return styled(f"{emoji} {label}", text, "brand")


class HelpSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label="Inicio", value="home", emoji="🏠", description="Resumen general")]
        for key, (emoji, label, desc, _) in HELP_PAGES.items():
            options.append(discord.SelectOption(label=label, value=key, emoji=emoji, description=desc[:100]))
        super().__init__(placeholder="📚 Elige una categoría...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if interaction.user.id != view.author_id:
            await interaction.response.send_message("❌ Este menú no es tuyo. Abre el tuyo con `N!help`.", ephemeral=True)
            return
        await interaction.response.edit_message(embed=help_embed(self.values[0]), view=view)


class HelpView(discord.ui.View):
    def __init__(self, author_id):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.add_item(HelpSelect())


@bot.tree.command(name="nexus-help", description="Menú de ayuda interactivo de Nexus Protect.")
async def nexus_help(interaction: discord.Interaction):
    await interaction.response.send_message(embed=help_embed("home"), view=HelpView(interaction.user.id), ephemeral=True)


@bot.command(name="help", aliases=["ayuda", "comandos", "nexus-help", "nexushelp"])
async def p_help(ctx):
    await ctx.send(embed=help_embed("home"), view=HelpView(ctx.author.id))


# =====================================================================
# PRESENCIA ROTATIVA
# =====================================================================
@tasks.loop(seconds=60)
async def rotate_presence():
    try:
        members = sum(g.member_count or 0 for g in bot.guilds)
        options = [
            discord.Game(name="N!help | /nexus-help"),
            discord.Activity(type=discord.ActivityType.watching, name=f"{len(bot.guilds)} servidores 🛡️"),
            discord.Activity(type=discord.ActivityType.watching, name=f"{members} miembros protegidos"),
        ]
        await bot.change_presence(activity=options[rotate_presence.current_loop % len(options)])
    except Exception as e:
        print(f"Error rotando presencia: {e}")


# =====================================================================
# EXTRAS 2: TRADUCTOR, ANTISCAM, FILTRO, HISTORIAL, NIVELES, SORTEOS,
# ROLES POR BOTONES, SUGERENCIAS, VOZ TEMPORAL, PÁNICO Y SERVER STATS
# =====================================================================
WARN_KICK_AT = 5     # 5 advertencias = expulsión automática
WARN_BAN_AT = 7      # 7 advertencias = baneo automático
MAX_TRANSLATE = 1000

TRANSLATE_LAST = {}
SUGGEST_LAST = {}
XP_COOLDOWN = {}
TEMPVOICE_LAST = {}
LEVELS_DIRTY = {"v": False}


def L(source, es, en):
    """Devuelve el texto en el idioma del usuario (Interaction, Context o Message)."""
    user = getattr(source, "user", None) or getattr(source, "author", None)
    lang = get_lang(user, getattr(source, "guild", None), getattr(source, "locale", None))
    return en if lang == "en" else es


def gsetting(guild, key, default=None):
    return DATA["settings"].get(str(guild.id), {}).get(key, default)


# =====================================================================
# HISTORIAL DE SANCIONES (CASOS) + AUTO-SANCIONES POR ADVERTENCIAS
# =====================================================================
CASE_ICONS = {"kick": "👢", "ban": "⛔", "unban": "✅", "timeout": "🔇", "warn": "⚠️"}


def add_case(guild_id, user_id, mod_id, kind, reason):
    cases = DATA["cases"].setdefault(str(guild_id), [])
    case_id = (cases[-1]["id"] + 1) if cases else 1
    cases.append({
        "id": case_id, "user": user_id, "mod": mod_id, "type": kind,
        "reason": (reason or "—")[:300], "date": int(time.time())
    })
    if len(cases) > 2000:
        del cases[:len(cases) - 2000]
    save_data()
    return case_id


def build_modlogs_embed(guild, user):
    cases = [c for c in DATA["cases"].get(str(guild.id), []) if c["user"] == user.id]
    if not cases:
        return styled("📋 Historial de sanciones", f"{user.mention} no tiene sanciones registradas.", "ok")
    lines = []
    for c in cases[-10:]:
        icon = CASE_ICONS.get(c["type"], "•")
        lines.append(f"`#{c['id']}` {icon} **{c['type']}** • <t:{c['date']}:R> • <@{c['mod']}>\n└ {c['reason'][:120]}")
    embed = styled(f"📋 Historial de {user}", "\n".join(lines), "warn")
    embed.set_footer(text=f"Mostrando las últimas {min(len(cases), 10)} de {len(cases)} sanciones")
    return embed


async def warn_escalation(guild, usuario, total):
    """Timeout cada 3 advertencias, expulsión a las 5 y baneo a las 7."""
    if total >= WARN_BAN_AT:
        try:
            await usuario.ban(reason=f"Nexus: {total} advertencias acumuladas")
            add_case(guild.id, usuario.id, bot.user.id, "ban", f"Automático: {total} advertencias")
            return f"\n⛔ **Baneado automáticamente** por acumular {total} advertencias."
        except discord.HTTPException:
            return "\n⚠️ Alcanzó el límite de baneo, pero no pude banearlo (jerarquía de roles)."
    if total == WARN_KICK_AT:
        try:
            await usuario.kick(reason=f"Nexus: {total} advertencias acumuladas")
            add_case(guild.id, usuario.id, bot.user.id, "kick", f"Automático: {total} advertencias")
            return f"\n👢 **Expulsado automáticamente** por acumular {total} advertencias."
        except discord.HTTPException:
            return "\n⚠️ Alcanzó el límite de expulsión, pero no pude expulsarlo (jerarquía de roles)."
    if total % WARN_TIMEOUT_AT == 0 and await timeout_member(usuario, 60, f"Nexus: {total} advertencias"):
        return "\n🔇 Silenciado **1 hora** por acumular advertencias."
    return ""


@bot.tree.command(name="modlogs", description="Muestra el historial de sanciones de un usuario.")
@app_commands.describe(usuario="Usuario a consultar")
@app_commands.checks.has_permissions(moderate_members=True)
async def modlogs_slash(interaction: discord.Interaction, usuario: discord.User):
    await interaction.response.send_message(embed=build_modlogs_embed(interaction.guild, usuario), ephemeral=True)


@bot.command(name="modlogs", aliases=["historial", "cases"])
@commands.has_permissions(moderate_members=True)
async def p_modlogs(ctx, usuario: discord.User):
    await say(ctx, embed=build_modlogs_embed(ctx.guild, usuario))


# =====================================================================
# TRADUCTOR (sin librerías extra: usa aiohttp)
# =====================================================================
LANG_ALIASES = {
    "es": "es", "español": "es", "espanol": "es", "spanish": "es",
    "en": "en", "ingles": "en", "inglés": "en", "english": "en",
    "fr": "fr", "frances": "fr", "francés": "fr", "french": "fr",
    "pt": "pt", "portugues": "pt", "portugués": "pt", "portuguese": "pt",
    "de": "de", "aleman": "de", "alemán": "de", "german": "de",
    "it": "it", "italiano": "it", "italian": "it",
    "ja": "ja", "jp": "ja", "japones": "ja", "japonés": "ja", "japanese": "ja",
    "ko": "ko", "coreano": "ko", "korean": "ko",
    "zh": "zh-CN", "chino": "zh-CN", "chinese": "zh-CN",
    "ru": "ru", "ruso": "ru", "russian": "ru",
    "ar": "ar", "arabe": "ar", "árabe": "ar", "arabic": "ar",
    "turco": "tr", "turkish": "tr", "hindi": "hi", "holandes": "nl", "dutch": "nl",
    "polaco": "pl", "polish": "pl", "ucraniano": "uk", "ukrainian": "uk",
    "indonesio": "id", "indonesian": "id", "latin": "la", "latín": "la",
}
LANG_NAMES = {
    "es": "Español", "en": "English", "fr": "Français", "pt": "Português", "de": "Deutsch",
    "it": "Italiano", "ja": "日本語", "ko": "한국어", "zh-CN": "中文", "zh-cn": "中文", "ru": "Русский",
    "ar": "العربية", "tr": "Türkçe", "hi": "हिन्दी", "nl": "Nederlands", "pl": "Polski",
    "uk": "Українська", "id": "Indonesia", "la": "Latina",
}


def resolve_lang(token):
    return LANG_ALIASES.get((token or "").strip().lower())


def parse_google_response(data):
    translated = "".join(part[0] for part in data[0] if part and part[0])
    detected = data[2] if len(data) > 2 and isinstance(data[2], str) else "auto"
    return translated, detected


async def translate_text(text, target):
    params = {"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text}
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get("https://translate.googleapis.com/translate_a/single", params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
    return parse_google_response(data)


def build_translation_embed(original, translated, src, tgt):
    embed = styled("🌐 Traducción / Translation", None, "brand", "Nexus Protect • Translator")
    embed.add_field(name=f"📥 {LANG_NAMES.get(src, src)}", value=original[:1000] or "—", inline=False)
    embed.add_field(name=f"📤 {LANG_NAMES.get(tgt, tgt)}", value=translated[:1000] or "—", inline=False)
    return embed


async def do_translate(source, text, target=None):
    text = (text or "").strip()
    if not text:
        return styled("❌ Error", L(source, "No hay texto para traducir.", "There is no text to translate."), "err")

    user = getattr(source, "user", None) or getattr(source, "author", None)
    now = time.time()
    if now - TRANSLATE_LAST.get(user.id, 0) < 3:
        return styled("⏳", L(source, "Espera unos segundos antes de traducir de nuevo.", "Wait a few seconds before translating again."), "warn")
    TRANSLATE_LAST[user.id] = now

    text = text[:MAX_TRANSLATE]
    user_lang = "en" if L(source, "es", "en") == "en" else "es"
    explicit = target is not None
    target = target or user_lang
    try:
        translated, detected = await translate_text(text, target)
        # Si no pidió idioma y el texto ya estaba en su idioma, se pasa al otro (es <-> en)
        if not explicit and detected.split("-")[0] == target.split("-")[0]:
            target = "en" if target == "es" else "es"
            translated, detected = await translate_text(text, target)
    except Exception as e:
        print(f"Error de traducción: {type(e).__name__}: {e}")
        return styled("❌ Error", L(source, "No pude traducir ahora mismo. Inténtalo de nuevo en un momento.",
                                   "I couldn't translate right now. Please try again in a moment."), "err")
    return build_translation_embed(text, translated, detected, target)


LANG_CHOICES = [app_commands.Choice(name=name, value=code) for code, name in (
    ("es", "Español"), ("en", "English"), ("fr", "Français"), ("pt", "Português"), ("de", "Deutsch"),
    ("it", "Italiano"), ("ja", "日本語"), ("ko", "한국어"), ("zh-CN", "中文"), ("ru", "Русский"), ("ar", "العربية"),
)]


@bot.tree.command(name="translate", description="Traduce un texto (por defecto a tu idioma).")
@app_commands.describe(texto="Texto a traducir", idioma="Idioma destino (opcional)")
@app_commands.choices(idioma=LANG_CHOICES)
async def translate_slash(interaction: discord.Interaction, texto: str, idioma: app_commands.Choice[str] = None):
    await interaction.response.defer(ephemeral=True)
    embed = await do_translate(interaction, texto, idioma.value if idioma else None)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.context_menu(name="Traducir / Translate")
async def translate_menu(interaction: discord.Interaction, message: discord.Message):
    await interaction.response.defer(ephemeral=True)
    text = message.content or "\n".join(e.description for e in message.embeds if e.description)
    embed = await do_translate(interaction, text)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.command(name="translate", aliases=["tr", "traducir", "traduce"])
async def p_translate(ctx, *, texto: str = None):
    target = None
    text = None
    ref = ctx.message.reference
    if ref and ref.message_id:
        try:
            replied = ref.resolved if isinstance(ref.resolved, discord.Message) else await ctx.channel.fetch_message(ref.message_id)
            text = replied.content or "\n".join(e.description for e in replied.embeds if e.description)
        except discord.HTTPException:
            text = None
        target = resolve_lang(texto) if texto else None
    else:
        if not texto:
            await say(ctx, "❌ Uso: `N!translate en hola mundo` o responde a un mensaje con `N!translate`.")
            return
        parts = texto.split(maxsplit=1)
        target = resolve_lang(parts[0]) if len(parts) == 2 else None
        text = parts[1] if target else texto
    async with ctx.typing():
        embed = await do_translate(ctx, text, target)
    await say(ctx, embed=embed)


# =====================================================================
# ANTI SCAM (enlaces de phishing) + FILTRO DE PALABRAS
# =====================================================================
LEGIT_DOMAINS = (
    "discord.com", "discord.gg", "discordapp.com", "discordapp.net", "discord.gift", "discord.media",
    "discordstatus.com", "discord.new", "dis.gd", "discord.co", "discordmerch.com", "discord.dev",
)
STEAM_LEGIT = ("steampowered.com", "steamcommunity.com", "steamstatic.com", "steamusercontent.com", "steamgames.com")
DISCORD_TYPOS = ("dicsord", "discrod", "dlscord", "disc0rd", "discorcl", "discorrd", "discordd",
                 "d1scord", "dlsc0rd", "discorb", "discorf", "dlscorcl")
SCAM_KEYWORDS = ("nitro", "gift", "free", "claim", "promo", "drop", "give", "partner", "reward", "gratis", "airdrop")
SHORTENERS = ("bit.ly", "tinyurl.com", "cutt.ly", "is.gd", "rb.gy", "shorturl.at", "ow.ly", "goo.gl",
              "rebrand.ly", "tiny.cc", "bit.do", "v.gd", "t.ly", "s.id")
SCAM_PHRASES = ("free nitro", "nitro gratis", "nitro free", "steam gift", "claim your", "has ganado", "you have been gifted",
                "you won", "airdrop", "gift from steam", "discord nitro for free")
DOMAIN_REGEX = re.compile(r"(?<![\w@.-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24})(?![\w-])", re.IGNORECASE)


def host_matches(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def defang(host):
    return host.replace(".", "[.]")


def detect_scam(content, new_member=False):
    text = (content or "").lower()
    hosts = [h.strip(".") for h in DOMAIN_REGEX.findall(text)]
    if not hosts:
        return None
    has_phrase = any(p in text for p in SCAM_PHRASES)
    suspicious = []
    for host in hosts:
        if host_matches(host, LEGIT_DOMAINS) or host_matches(host, STEAM_LEGIT):
            continue
        suspicious.append(host)
        if any(t in host for t in DISCORD_TYPOS):
            return f"dominio que imita a Discord ({defang(host)})"
        if "discord" in host and any(k in host for k in SCAM_KEYWORDS):
            return f"dominio sospechoso de Discord ({defang(host)})"
        if any(d in host for d in ("discord.com", "discordapp.com", "discord.gg", "steampowered.com", "steamcommunity.com")):
            return f"dominio falso con dominio legítimo incrustado ({defang(host)})"
        if "steam" in host and any(k in host for k in ("gift", "free", "trade", "wallet", "promo", "commun", "comrnun", "drop", "nitro", "give")):
            return f"dominio que imita a Steam ({defang(host)})"
        if "nitro" in host and any(k in host for k in ("free", "gift", "claim", "drop", "give", "gratis", "promo")):
            return f"dominio de falso Nitro ({defang(host)})"
        if host_matches(host, SHORTENERS) and (has_phrase or new_member):
            return f"enlace acortado sospechoso ({defang(host)})"
    if suspicious and has_phrase:
        return f"frase de estafa con enlace ({defang(suspicious[0])})"
    return None


def normalize_text(s):
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


WORD_CACHE = {}


def word_regex(guild_id):
    words = tuple(DATA["words"].get(str(guild_id), []))
    cached = WORD_CACHE.get(guild_id)
    if cached and cached[0] == words:
        return cached[1]
    regex = None
    if words:
        pattern = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        regex = re.compile(r"(?<!\w)(?:" + pattern + r")(?!\w)")
    WORD_CACHE[guild_id] = (words, regex)
    return regex


def match_filtered_word(guild_id, content):
    regex = word_regex(guild_id)
    if not regex:
        return None
    m = regex.search(normalize_text(content))
    return m.group(0) if m else None


def message_exempt(message):
    member = message.author
    return (message.author.bot or message.guild is None or not isinstance(member, discord.Member)
            or member.guild_permissions.administrator or is_trusted(message.guild, member))


async def extra_message_checks(message):
    guild = message.guild
    member = message.author
    channel = message.channel
    if not message.content:
        return False

    if module_enabled(guild.id, "antiscam"):
        joined = getattr(member, "joined_at", None)
        new_member = bool(joined and (discord.utils.utcnow() - joined).total_seconds() < 86400)
        reason = detect_scam(message.content, new_member)
        if reason:
            await delete_quietly(message)
            muted = await timeout_member(member, 30, "Nexus AntiScam")
            await warn_in_channel(channel, L(message,
                f"🚨 {member.mention}, tu mensaje fue eliminado por contener un enlace peligroso.",
                f"🚨 {member.mention}, your message was removed for containing a dangerous link."))
            await send_log(guild, "🚨 AntiScam • Enlace Peligroso",
                           f"👤 **Usuario:** {member.mention} (`{member.id}`)\n💬 **Canal:** {channel.mention}\n"
                           f"🔎 **Motivo:** {reason}\n🔇 Timeout 30 min: {'✅' if muted else '❌ (sin permisos)'}\n"
                           f"⚠️ Si su cuenta fue hackeada, avísale.",
                           color=discord.Color.dark_red())
            return True

    if module_enabled(guild.id, "wordfilter"):
        word = match_filtered_word(guild.id, message.content)
        if word:
            await delete_quietly(message)
            await warn_in_channel(channel, L(message,
                f"🧼 {member.mention}, ese lenguaje no está permitido aquí.",
                f"🧼 {member.mention}, that language isn't allowed here."))
            await send_log(guild, "🧼 Filtro de Palabras",
                           f"👤 **Usuario:** {member.mention} (`{member.id}`)\n💬 **Canal:** {channel.mention}\n"
                           f"🔎 **Palabra detectada:** ||{word}||",
                           color=discord.Color.orange())
            return True
    return False


@bot.listen("on_message_edit")
async def extra_edit_checks(before, after):
    if after.guild is None or before.content == after.content or message_exempt(after):
        return
    await extra_message_checks(after)


def wf_add(guild, palabra):
    word = normalize_text(palabra).strip()
    words = DATA["words"].setdefault(str(guild.id), [])
    if len(word) < 2 or len(word) > 60:
        return styled("❌ Error", "La palabra debe tener entre 2 y 60 caracteres.", "err")
    if word in words:
        return styled("ℹ️ Ya existe", "Esa palabra ya está en el filtro.", "warn")
    if len(words) >= 100:
        return styled("❌ Límite", "El filtro admite hasta 100 palabras.", "err")
    words.append(word)
    save_data()
    return styled("✅ Palabra añadida", f"Se añadió al filtro (total: **{len(words)}**). Recuerda activar el módulo con `N!config wordfilter on` si lo tienes apagado.", "ok")


def wf_remove(guild, palabra):
    word = normalize_text(palabra).strip()
    words = DATA["words"].setdefault(str(guild.id), [])
    if word not in words:
        return styled("❌ Error", "Esa palabra no está en el filtro.", "err")
    words.remove(word)
    save_data()
    return styled("🗑️ Palabra quitada", f"Total en el filtro: **{len(words)}**.", "ok")


def wf_list(guild):
    words = DATA["words"].get(str(guild.id), [])
    text = "\n".join(f"||{w}||" for w in words) if words else "El filtro está vacío."
    return styled("🧼 Filtro de palabras", text[:3900], "info", f"{len(words)} palabras • se muestran ocultas con spoiler")


wordfilter_group = app_commands.Group(name="wordfilter", description="Filtro de palabras prohibidas.")
bot.tree.add_command(wordfilter_group)


@wordfilter_group.command(name="add", description="Añade una palabra al filtro.")
@app_commands.describe(palabra="Palabra o frase a bloquear")
@app_commands.checks.has_permissions(manage_guild=True)
async def wf_add_slash(interaction: discord.Interaction, palabra: str):
    await interaction.response.send_message(embed=wf_add(interaction.guild, palabra), ephemeral=True)


@wordfilter_group.command(name="remove", description="Quita una palabra del filtro.")
@app_commands.describe(palabra="Palabra a quitar")
@app_commands.checks.has_permissions(manage_guild=True)
async def wf_remove_slash(interaction: discord.Interaction, palabra: str):
    await interaction.response.send_message(embed=wf_remove(interaction.guild, palabra), ephemeral=True)


@wordfilter_group.command(name="list", description="Muestra las palabras del filtro.")
@app_commands.checks.has_permissions(manage_guild=True)
async def wf_list_slash(interaction: discord.Interaction):
    await interaction.response.send_message(embed=wf_list(interaction.guild), ephemeral=True)


@bot.group(name="wordfilter", aliases=["wf", "filtro"], invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def p_wordfilter(ctx):
    await say(ctx, "Uso: `N!wordfilter add <palabra>` • `N!wordfilter remove <palabra>` • `N!wordfilter list`")


@p_wordfilter.command(name="add")
@commands.has_permissions(manage_guild=True)
async def p_wf_add(ctx, *, palabra: str):
    await delete_quietly(ctx.message)  # no dejar la palabra a la vista
    await say(ctx, embed=wf_add(ctx.guild, palabra), delete_after=10)


@p_wordfilter.command(name="remove", aliases=["del"])
@commands.has_permissions(manage_guild=True)
async def p_wf_remove(ctx, *, palabra: str):
    await delete_quietly(ctx.message)
    await say(ctx, embed=wf_remove(ctx.guild, palabra), delete_after=10)


@p_wordfilter.command(name="list")
@commands.has_permissions(manage_guild=True)
async def p_wf_list(ctx):
    await say(ctx, embed=wf_list(ctx.guild), delete_after=30)


# =====================================================================
# EDAD MÍNIMA DE CUENTA (anti cuentas alt)
# =====================================================================
async def check_min_age(member):
    guild = member.guild
    min_age = gsetting(guild, "min_age", 0)
    if not min_age or is_trusted(guild, member):
        return False
    age = (discord.utils.utcnow() - member.created_at).days
    if age >= min_age:
        return False
    try:
        await member.send(embed=styled(
            "🛡️ Cuenta demasiado nueva / Account too new",
            f"🇪🇸 Este servidor exige cuentas con al menos **{min_age}** días. La tuya tiene **{age}**. Vuelve más adelante.\n\n"
            f"🇺🇸 This server requires accounts at least **{min_age}** days old. Yours is **{age}**. Please come back later.",
            "warn"))
    except discord.HTTPException:
        pass
    try:
        await member.kick(reason=f"Nexus MinAge: cuenta de {age} días (mínimo {min_age})")
    except discord.HTTPException:
        return False
    await send_log(guild, "🛡️ Edad Mínima • Cuenta Expulsada",
                   f"👤 {member.mention} (`{member.id}`) — cuenta de **{age}** días (mínimo **{min_age}**).",
                   color=discord.Color.orange())
    return True


def do_minage(guild, dias):
    if dias <= 0:
        apply_setting(guild, "min_age", None)
        return styled("🔴 Edad mínima desactivada", "Ya no se filtran cuentas por antigüedad.", "warn")
    apply_setting(guild, "min_age", dias)
    return styled("✅ Edad mínima configurada", f"Se expulsará a las cuentas con menos de **{dias}** días al entrar.", "ok")


@bot.tree.command(name="minage", description="Edad mínima de cuenta para entrar (0 = desactivar).")
@app_commands.describe(dias="Días de antigüedad mínima (0-365)")
@app_commands.checks.has_permissions(manage_guild=True)
async def minage_slash(interaction: discord.Interaction, dias: app_commands.Range[int, 0, 365]):
    await interaction.response.send_message(embed=do_minage(interaction.guild, dias), ephemeral=True)


@bot.command(name="minage", aliases=["edadminima"])
@commands.has_permissions(manage_guild=True)
async def p_minage(ctx, dias: commands.Range[int, 0, 365]):
    await say(ctx, embed=do_minage(ctx.guild, dias))


# =====================================================================
# VERIFICACIÓN CON CÓDIGO (captcha simple)
# =====================================================================
class VerifyModal(discord.ui.Modal):
    def __init__(self, code, role_id):
        super().__init__(title="Verificación / Verification", timeout=180)
        self.code = code
        self.role_id = role_id
        self.answer = discord.ui.TextInput(
            label=f"Escribe / Type: {code}"[:45], min_length=len(code), max_length=len(code), required=True
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction):
        txt = VERIFY_TEXTS[get_lang(interaction.user, interaction.guild, interaction.locale)]
        if self.answer.value.strip().upper() != self.code:
            await interaction.response.send_message(
                L(interaction, "❌ Código incorrecto. Presiona el botón otra vez para recibir uno nuevo.",
                  "❌ Wrong code. Press the button again to get a new one."), ephemeral=True)
            return
        role = interaction.guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message(txt["no_role"], ephemeral=True)
            return
        await give_verify_role(interaction, role, txt)


async def give_verify_role(interaction, role, txt):
    # Se revisa también al hacer clic: si el rol se volvió peligroso, no se entrega
    if has_dangerous_perms(role) or role.managed or role >= interaction.guild.me.top_role:
        await interaction.response.send_message(txt["fail"], ephemeral=True)
        return
    try:
        await interaction.user.add_roles(role, reason="Nexus Verify")
    except discord.HTTPException:
        await interaction.response.send_message(txt["fail"], ephemeral=True)
        return
    await interaction.response.send_message(txt["done"], ephemeral=True)


def start_captcha(interaction, role):
    code = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=5))
    return VerifyModal(code, role.id)


# =====================================================================
# ROLES POR BOTONES
# =====================================================================
class RoleButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rr:(?P<role_id>\d+)"):
    def __init__(self, role_id, label="Rol"):
        super().__init__(discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary, custom_id=f"rr:{role_id}"))
        self.role_id = role_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match, /):
        return cls(int(match["role_id"]), item.label or "Rol")

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        member = interaction.user
        if role is None:
            await interaction.response.send_message(L(interaction, "❌ Ese rol ya no existe.", "❌ That role no longer exists."), ephemeral=True)
            return
        if has_dangerous_perms(role) or role.managed or role >= interaction.guild.me.top_role:
            await interaction.response.send_message(L(interaction, "❌ No puedo gestionar ese rol.", "❌ I can't manage that role."), ephemeral=True)
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Nexus RolePanel")
                msg = L(interaction, f"➖ Te quité el rol **{role.name}**.", f"➖ Removed the **{role.name}** role.")
            else:
                await member.add_roles(role, reason="Nexus RolePanel")
                msg = L(interaction, f"➕ Ahora tienes el rol **{role.name}**.", f"➕ You now have the **{role.name}** role.")
        except discord.HTTPException:
            msg = L(interaction, "❌ No pude cambiar tu rol. Avisa a un administrador.", "❌ I couldn't change your role. Tell an administrator.")
        await interaction.response.send_message(msg, ephemeral=True)


async def build_role_panel(channel, titulo, roles):
    clean, errors = [], []
    for r in roles:
        if r in clean:
            continue
        err = validate_safe_role(r)
        if err:
            errors.append(f"{r.mention}: {err}")
        else:
            clean.append(r)
    if not clean:
        return styled("❌ Error", "\n".join(errors) or "Indica al menos un rol.", "err")
    view = discord.ui.View(timeout=None)
    for r in clean[:10]:
        view.add_item(RoleButton(r.id, r.name))
    embed = styled(f"🎭 {titulo.strip()[:200]}",
                   "🇪🇸 Presiona un botón para obtener o quitarte el rol.\n🇺🇸 Press a button to get or remove the role.",
                   "brand", "Nexus Protect • Roles")
    try:
        await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        return styled("❌ Error", "No tengo permisos para escribir en este canal.", "err")
    note = ("\n\n⚠️ Omitidos:\n" + "\n".join(errors)) if errors else ""
    return styled("✅ Panel de roles creado", f"Roles: {' '.join(r.mention for r in clean[:10])}{note}", "ok")


@bot.tree.command(name="rolepanel", description="Crea un panel con botones para que los miembros se den roles.")
@app_commands.describe(titulo="Título del panel", rol1="Rol 1", rol2="Rol 2", rol3="Rol 3", rol4="Rol 4", rol5="Rol 5")
@app_commands.checks.has_permissions(manage_roles=True)
async def rolepanel_slash(interaction: discord.Interaction, titulo: str, rol1: discord.Role,
                          rol2: discord.Role = None, rol3: discord.Role = None,
                          rol4: discord.Role = None, rol5: discord.Role = None):
    await interaction.response.defer(ephemeral=True)
    roles = [r for r in (rol1, rol2, rol3, rol4, rol5) if r]
    result = await build_role_panel(interaction.channel, titulo, roles)
    await interaction.followup.send(embed=result, ephemeral=True)


@bot.command(name="rolepanel", aliases=["panelroles"])
@commands.has_permissions(manage_roles=True)
async def p_rolepanel(ctx, titulo: str, roles: commands.Greedy[discord.Role]):
    if not roles:
        await say(ctx, '❌ Uso: `N!rolepanel "Título" @rol1 @rol2 ...` (hasta 10 roles)')
        return
    await say(ctx, embed=await build_role_panel(ctx.channel, titulo, roles))


# =====================================================================
# SORTEOS (GIVEAWAYS)
# =====================================================================
def giveaway_embed(g, ended=False):
    host = f"<@{g['host']}>"
    if ended:
        winners = g.get("winner_ids", [])
        who = " ".join(f"<@{w}>" for w in winners) if winners else "Nadie participó 😕"
        return styled("🎉 SORTEO FINALIZADO",
                      f"🏆 **Premio:** {g['prize']}\n👑 **Ganador(es):** {who}\n🎟️ **Participantes:** {len(g['entries'])}\n👤 **Organiza:** {host}",
                      "warn", "Nexus Protect • Giveaway")
    return styled("🎉 ¡SORTEO! / GIVEAWAY!",
                  f"🏆 **Premio / Prize:** {g['prize']}\n⏰ **Termina / Ends:** <t:{g['end']}:R>\n"
                  f"👑 **Ganadores / Winners:** {g['winners']}\n👤 **Organiza / Host:** {host}\n\n"
                  "Presiona el botón para participar.\nPress the button to join.",
                  "brand", "Nexus Protect • Giveaway")


class GiveawayView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎉 Participar / Join", style=discord.ButtonStyle.success, custom_id="giveaway_join_btn")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = DATA["giveaways"].get(str(interaction.message.id))
        if not g or g.get("ended"):
            await interaction.response.send_message(L(interaction, "❌ Este sorteo ya terminó.", "❌ This giveaway has ended."), ephemeral=True)
            return
        uid = interaction.user.id
        if uid in g["entries"]:
            g["entries"].remove(uid)
            msg = L(interaction, "➖ Saliste del sorteo.", "➖ You left the giveaway.")
        else:
            g["entries"].append(uid)
            msg = L(interaction, "✅ ¡Estás participando! Suerte 🍀 (presiona de nuevo para salir)",
                    "✅ You're in! Good luck 🍀 (press again to leave)")
        save_data()
        await interaction.response.send_message(msg, ephemeral=True)


async def create_giveaway(channel, host, duration_s, winners, prize):
    g = {"guild": channel.guild.id, "channel": channel.id, "prize": prize.strip()[:200], "winners": winners,
         "end": int(time.time()) + duration_s, "host": host.id, "entries": [], "ended": False, "winner_ids": []}
    msg = await channel.send(embed=giveaway_embed(g), view=GiveawayView())
    DATA["giveaways"][str(msg.id)] = g
    save_data()
    return msg


async def finish_giveaway(msg_id, reroll=False):
    g = DATA["giveaways"].get(str(msg_id))
    if not g:
        return None
    guild = bot.get_guild(g["guild"])
    channel = guild.get_channel(g["channel"]) if guild else None
    eligible = [u for u in g["entries"] if guild and guild.get_member(u)]
    if reroll:
        fresh = [u for u in eligible if u not in g.get("winner_ids", [])]
        eligible = fresh or eligible
    k = min(g["winners"], len(eligible))
    winners = random.sample(eligible, k) if k else []
    g["ended"] = True
    g["winner_ids"] = winners
    g["ended_at"] = int(time.time())
    save_data()
    if channel:
        message = None
        try:
            message = await channel.fetch_message(int(msg_id))
            await message.edit(embed=giveaway_embed(g, ended=True), view=None)
        except discord.HTTPException:
            pass
        mentions = " ".join(f"<@{w}>" for w in winners)
        text = (f"🎉 ¡Felicidades {mentions}! Ganaste **{g['prize']}**" if winners
                else f"😕 Nadie participó en el sorteo de **{g['prize']}**.")
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions(users=True),
                               reference=message if message else None)
        except discord.HTTPException:
            pass
    return winners


@tasks.loop(seconds=20)
async def giveaway_loop():
    now = int(time.time())
    for msg_id, g in list(DATA["giveaways"].items()):
        try:
            if not g.get("ended") and g["end"] <= now:
                await finish_giveaway(msg_id)
            elif g.get("ended") and now - g.get("ended_at", now) > 7 * 86400:
                DATA["giveaways"].pop(msg_id, None)
                save_data()
        except Exception as e:
            print(f"Error en sorteo {msg_id}: {type(e).__name__}: {e}")


def giveaway_duration_error(seconds):
    if seconds is None or seconds < 10 or seconds > 30 * 86400:
        return styled("❌ Tiempo inválido", "Usa entre `10s` y `30d`. Ejemplos: `30m`, `2h`, `1d`.", "err")
    return None


@bot.tree.command(name="giveaway", description="Crea un sorteo en este canal.")
@app_commands.describe(duracion="Ej: 30m, 2h, 1d", premio="Qué se sortea", ganadores="Cantidad de ganadores (1-10)")
@app_commands.checks.has_permissions(manage_guild=True)
async def giveaway_slash(interaction: discord.Interaction, duracion: str, premio: str,
                         ganadores: app_commands.Range[int, 1, 10] = 1):
    seconds = parse_duration(duracion)
    error = giveaway_duration_error(seconds)
    if error:
        await interaction.response.send_message(embed=error, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await create_giveaway(interaction.channel, interaction.user, seconds, ganadores, premio)
    except discord.Forbidden:
        await interaction.followup.send(tr(interaction, "bot_no_perm"), ephemeral=True)
        return
    await interaction.followup.send("✅ Sorteo creado.", ephemeral=True)


@bot.command(name="giveaway", aliases=["sorteo", "gstart"])
@commands.has_permissions(manage_guild=True)
async def p_giveaway(ctx, duracion: str, ganadores: commands.Range[int, 1, 10], *, premio: str):
    seconds = parse_duration(duracion)
    error = giveaway_duration_error(seconds)
    if error:
        await say(ctx, embed=error)
        return
    try:
        await create_giveaway(ctx.channel, ctx.author, seconds, ganadores, premio)
    except discord.Forbidden:
        await say(ctx, pmsg(ctx, "bot_no_perm"))
        return
    await delete_quietly(ctx.message)


async def giveaway_admin(guild, message_id, action):
    g = DATA["giveaways"].get(str(message_id))
    if not g or g["guild"] != guild.id:
        return styled("❌ Error", "No encontré ese sorteo en este servidor (revisa el ID del mensaje).", "err")
    if action == "end":
        if g.get("ended"):
            return styled("ℹ️", "Ese sorteo ya terminó. Usa `greroll` para elegir otro ganador.", "warn")
        await finish_giveaway(message_id)
        return styled("✅ Sorteo finalizado", "Se eligieron los ganadores.", "ok")
    if not g.get("ended"):
        return styled("ℹ️", "Ese sorteo sigue activo. Termínalo primero con `gend`.", "warn")
    await finish_giveaway(message_id, reroll=True)
    return styled("🎲 Nuevo sorteo realizado", "Se eligieron nuevos ganadores.", "ok")


@bot.tree.command(name="gend", description="Termina un sorteo antes de tiempo.")
@app_commands.describe(mensaje_id="ID del mensaje del sorteo")
@app_commands.checks.has_permissions(manage_guild=True)
async def gend_slash(interaction: discord.Interaction, mensaje_id: str):
    await interaction.response.defer(ephemeral=True)
    uid = parse_user_id(mensaje_id)
    result = await giveaway_admin(interaction.guild, uid, "end") if uid else styled("❌ Error", "ID inválido.", "err")
    await interaction.followup.send(embed=result, ephemeral=True)


@bot.tree.command(name="greroll", description="Elige nuevos ganadores de un sorteo terminado.")
@app_commands.describe(mensaje_id="ID del mensaje del sorteo")
@app_commands.checks.has_permissions(manage_guild=True)
async def greroll_slash(interaction: discord.Interaction, mensaje_id: str):
    await interaction.response.defer(ephemeral=True)
    uid = parse_user_id(mensaje_id)
    result = await giveaway_admin(interaction.guild, uid, "reroll") if uid else styled("❌ Error", "ID inválido.", "err")
    await interaction.followup.send(embed=result, ephemeral=True)


@bot.command(name="gend")
@commands.has_permissions(manage_guild=True)
async def p_gend(ctx, mensaje_id: str):
    uid = parse_user_id(mensaje_id)
    await say(ctx, embed=await giveaway_admin(ctx.guild, uid, "end") if uid else styled("❌ Error", "ID inválido.", "err"))


@bot.command(name="greroll")
@commands.has_permissions(manage_guild=True)
async def p_greroll(ctx, mensaje_id: str):
    uid = parse_user_id(mensaje_id)
    await say(ctx, embed=await giveaway_admin(ctx.guild, uid, "reroll") if uid else styled("❌ Error", "ID inválido.", "err"))


# =====================================================================
# NIVELES Y XP (se activa con: N!config levels on)
# =====================================================================
def xp_needed(level):
    return 5 * level ** 2 + 50 * level + 100


def level_info(total_xp):
    level, xp = 0, total_xp
    while xp >= xp_needed(level):
        xp -= xp_needed(level)
        level += 1
    return level, xp, xp_needed(level)


def progress_bar(current, total, size=10):
    filled = int(size * current / total) if total else 0
    return "▰" * filled + "▱" * (size - filled)


async def grant_level_roles(member, level):
    mapping = gsetting(member.guild, "level_roles", {}) or {}
    for lvl, role_id in mapping.items():
        if int(lvl) <= level:
            role = member.guild.get_role(int(role_id))
            if role and role not in member.roles and not validate_safe_role(role):
                try:
                    await member.add_roles(role, reason=f"Nexus Niveles: nivel {lvl}")
                except discord.HTTPException:
                    pass


async def award_xp(message):
    guild = message.guild
    member = message.author
    if not isinstance(member, discord.Member) or not module_enabled(guild.id, "levels"):
        return
    content = message.content.strip()
    if len(content) < 3 or content.lower().startswith("n!") or content.startswith(bot.user.mention):
        return
    key = (guild.id, member.id)
    now = time.time()
    if now - XP_COOLDOWN.get(key, 0) < 60:
        return
    XP_COOLDOWN[key] = now
    guild_levels = DATA["levels"].setdefault(str(guild.id), {})
    before = guild_levels.get(str(member.id), 0)
    after = before + random.randint(15, 25)
    guild_levels[str(member.id)] = after
    LEVELS_DIRTY["v"] = True
    new_level = level_info(after)[0]
    if new_level > level_info(before)[0]:
        try:
            await message.channel.send(
                embed=styled("🎉 ¡Subiste de nivel!", f"{member.mention} alcanzó el nivel **{new_level}** 🚀", "ok", "Nexus Protect • Levels"),
                allowed_mentions=discord.AllowedMentions(users=True), delete_after=20)
        except discord.HTTPException:
            pass
        await grant_level_roles(member, new_level)


@tasks.loop(minutes=5)
async def levels_flush_loop():
    try:
        cutoff = time.time() - 120
        for key in [k for k, t in XP_COOLDOWN.items() if t < cutoff]:
            XP_COOLDOWN.pop(key, None)
        if LEVELS_DIRTY["v"]:
            LEVELS_DIRTY["v"] = False
            save_data()
    except Exception as e:
        print(f"Error guardando niveles: {type(e).__name__}: {e}")


def build_rank_embed(guild, member):
    guild_levels = DATA["levels"].get(str(guild.id), {})
    total = guild_levels.get(str(member.id), 0)
    level, xp, need = level_info(total)
    ranking = sorted(guild_levels.items(), key=lambda kv: kv[1], reverse=True)
    position = next((i for i, (uid, _) in enumerate(ranking, 1) if uid == str(member.id)), None)
    embed = styled(f"📈 Nivel de {member.display_name}",
                   f"🏅 **Nivel:** {level}\n✨ **XP:** {xp}/{need}\n{progress_bar(xp, need)}\n"
                   f"🏆 **Puesto:** {'#' + str(position) if position else 'sin clasificar'}\n💬 **XP total:** {total}",
                   "brand", "Nexus Protect • Levels")
    embed.set_thumbnail(url=member.display_avatar.url)
    if not module_enabled(guild.id, "levels"):
        embed.add_field(name="ℹ️", value="El sistema de niveles está apagado. Un admin puede activarlo con `N!config levels on`.", inline=False)
    return embed


def build_leaderboard_embed(guild):
    ranking = sorted(DATA["levels"].get(str(guild.id), {}).items(), key=lambda kv: kv[1], reverse=True)[:10]
    if not ranking:
        return styled("🏆 Ranking", "Aún no hay datos de actividad.", "warn")
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"{medals[i] if i < 3 else f'`{i + 1}.`'} <@{uid}> — nivel **{level_info(xp)[0]}** ({xp} XP)"
             for i, (uid, xp) in enumerate(ranking)]
    return styled(f"🏆 Ranking de {guild.name}", "\n".join(lines), "brand", "Nexus Protect • Levels")


def do_levelrole(guild, nivel, rol):
    mapping = dict(gsetting(guild, "level_roles", {}) or {})
    if rol is None:
        mapping.pop(str(nivel), None)
        apply_setting(guild, "level_roles", mapping or None)
        return styled("🗑️ Rol de nivel quitado", f"Ya no se entrega rol al llegar al nivel **{nivel}**.", "warn")
    error = validate_safe_role(rol)
    if error:
        return styled("❌ Error", error, "err")
    mapping[str(nivel)] = rol.id
    apply_setting(guild, "level_roles", mapping)
    return styled("✅ Rol de nivel configurado", f"Al llegar al nivel **{nivel}** se entregará {rol.mention}.", "ok")


@bot.tree.command(name="rank", description="Muestra tu nivel y XP (o el de otro usuario).")
@app_commands.describe(usuario="Usuario (por defecto, tú)")
async def rank_slash(interaction: discord.Interaction, usuario: discord.Member = None):
    await interaction.response.send_message(embed=build_rank_embed(interaction.guild, usuario or interaction.user))


@bot.command(name="rank", aliases=["nivel", "level"])
async def p_rank(ctx, usuario: discord.Member = None):
    await ctx.send(embed=build_rank_embed(ctx.guild, usuario or ctx.author))


@bot.tree.command(name="leaderboard", description="Ranking de los miembros más activos.")
async def leaderboard_slash(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_leaderboard_embed(interaction.guild))


@bot.command(name="leaderboard", aliases=["top", "ranking", "lb"])
async def p_leaderboard(ctx):
    await ctx.send(embed=build_leaderboard_embed(ctx.guild), allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name="levelrole", description="Rol que se entrega al llegar a un nivel (sin rol = quitar).")
@app_commands.describe(nivel="Nivel (1-200)", rol="Rol a entregar")
@app_commands.checks.has_permissions(manage_guild=True)
async def levelrole_slash(interaction: discord.Interaction, nivel: app_commands.Range[int, 1, 200], rol: discord.Role = None):
    await interaction.response.send_message(embed=do_levelrole(interaction.guild, nivel, rol), ephemeral=True)


@bot.command(name="levelrole", aliases=["rolnivel"])
@commands.has_permissions(manage_guild=True)
async def p_levelrole(ctx, nivel: commands.Range[int, 1, 200], rol: discord.Role = None):
    await say(ctx, embed=do_levelrole(ctx.guild, nivel, rol))


# =====================================================================
# SUGERENCIAS
# =====================================================================
def do_setsuggest(guild, canal):
    apply_setting(guild, "suggest_channel", canal.id if canal else None)
    if canal:
        return styled("✅ Canal de sugerencias configurado", f"Las sugerencias se publicarán en {canal.mention}.", "ok")
    return styled("🔴 Sugerencias desactivadas", "Ya no se aceptan sugerencias.", "warn")


async def post_suggestion(source, user, guild, texto):
    channel_id = gsetting(guild, "suggest_channel")
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if not channel:
        return styled("❌ Error", L(source, "Este servidor no tiene canal de sugerencias configurado.",
                                    "This server has no suggestions channel configured."), "err")
    now = time.time()
    if now - SUGGEST_LAST.get(user.id, 0) < 30:
        return styled("⏳", L(source, "Espera 30 segundos entre sugerencias.", "Wait 30 seconds between suggestions."), "warn")
    SUGGEST_LAST[user.id] = now
    embed = styled("💡 Nueva sugerencia", texto.strip()[:2000], "brand", f"Sugerencia de {user.display_name}")
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.add_field(name="Estado", value="🕐 Pendiente")
    try:
        msg = await channel.send(embed=embed)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
    except discord.HTTPException:
        return styled("❌ Error", L(source, "No pude publicar en el canal de sugerencias.", "I couldn't post in the suggestions channel."), "err")
    return styled("✅ " + L(source, "Sugerencia enviada", "Suggestion sent"),
                  L(source, f"Tu sugerencia fue publicada en {channel.mention}.", f"Your suggestion was posted in {channel.mention}."), "ok")


async def review_suggestion(guild, message_id, aprobar, razon, mod):
    channel_id = gsetting(guild, "suggest_channel")
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if not channel:
        return styled("❌ Error", "No hay canal de sugerencias configurado.", "err")
    try:
        msg = await channel.fetch_message(message_id)
    except discord.HTTPException:
        return styled("❌ Error", "No encontré esa sugerencia en el canal (revisa el ID del mensaje).", "err")
    if msg.author.id != bot.user.id or not msg.embeds:
        return styled("❌ Error", "Ese mensaje no es una sugerencia mía.", "err")
    new = discord.Embed.from_dict(msg.embeds[0].to_dict())
    new.color = COLORS["ok"] if aprobar else COLORS["err"]
    new.clear_fields()
    state = "✅ Aprobada" if aprobar else "❌ Rechazada"
    new.add_field(name="Estado", value=f"{state} por {mod.mention}\n📝 {razon[:500]}")
    try:
        await msg.edit(embed=new)
    except discord.HTTPException:
        return styled("❌ Error", "No pude editar esa sugerencia.", "err")
    return styled("✅ Sugerencia actualizada", f"Estado: **{state}**", "ok")


@bot.tree.command(name="setsuggest", description="Elige el canal de sugerencias (sin canal = desactivar).")
@app_commands.describe(canal="Canal de sugerencias")
@app_commands.checks.has_permissions(manage_guild=True)
async def setsuggest_slash(interaction: discord.Interaction, canal: discord.TextChannel = None):
    await interaction.response.send_message(embed=do_setsuggest(interaction.guild, canal), ephemeral=True)


@bot.command(name="setsuggest", aliases=["setsugerencias"])
@commands.has_permissions(manage_guild=True)
async def p_setsuggest(ctx, canal: discord.TextChannel = None):
    await say(ctx, embed=do_setsuggest(ctx.guild, canal))


@bot.tree.command(name="suggest", description="Envía una sugerencia al servidor.")
@app_commands.describe(texto="Tu sugerencia")
async def suggest_slash(interaction: discord.Interaction, texto: str):
    await interaction.response.defer(ephemeral=True)
    result = await post_suggestion(interaction, interaction.user, interaction.guild, texto)
    await interaction.followup.send(embed=result, ephemeral=True)


@bot.command(name="suggest", aliases=["sugerir", "sugerencia"])
async def p_suggest(ctx, *, texto: str):
    result = await post_suggestion(ctx, ctx.author, ctx.guild, texto)
    await delete_quietly(ctx.message)
    await say(ctx, embed=result, delete_after=10)


@bot.tree.command(name="suggestion-review", description="Aprueba o rechaza una sugerencia.")
@app_commands.describe(mensaje_id="ID del mensaje de la sugerencia", estado="Resultado", razon="Razón")
@app_commands.choices(estado=[app_commands.Choice(name="Aprobar", value="ok"), app_commands.Choice(name="Rechazar", value="no")])
@app_commands.checks.has_permissions(manage_guild=True)
async def suggestion_review_slash(interaction: discord.Interaction, mensaje_id: str, estado: app_commands.Choice[str], razon: str = "Sin razón"):
    await interaction.response.defer(ephemeral=True)
    uid = parse_user_id(mensaje_id)
    result = (await review_suggestion(interaction.guild, uid, estado.value == "ok", razon, interaction.user)
              if uid else styled("❌ Error", "ID inválido.", "err"))
    await interaction.followup.send(embed=result, ephemeral=True)


@bot.command(name="suggestion", aliases=["sug"])
@commands.has_permissions(manage_guild=True)
async def p_suggestion(ctx, accion: str, mensaje_id: str, *, razon: str = "Sin razón"):
    accion = accion.lower()
    if accion not in ("aprobar", "approve", "rechazar", "deny"):
        await say(ctx, "❌ Uso: `N!suggestion aprobar|rechazar <id del mensaje> [razón]`")
        return
    uid = parse_user_id(mensaje_id)
    result = (await review_suggestion(ctx.guild, uid, accion in ("aprobar", "approve"), razon, ctx.author)
              if uid else styled("❌ Error", "ID inválido.", "err"))
    await say(ctx, embed=result)


# =====================================================================
# DESPEDIDA
# =====================================================================
def do_setgoodbye(guild, canal):
    apply_setting(guild, "goodbye_channel", canal.id if canal else None)
    if canal:
        return styled("✅ Despedida configurada", f"Los mensajes de despedida se enviarán en {canal.mention}.", "ok")
    return styled("🔴 Despedida desactivada", "Ya no se enviarán mensajes de despedida.", "warn")


async def handle_goodbye(member):
    guild = member.guild
    channel_id = gsetting(guild, "goodbye_channel")
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if not channel:
        return
    embed = styled("👋 Hasta pronto", f"**{member.display_name}** salió del servidor.\nAhora somos **{guild.member_count}** miembros.", "warn", "Nexus Protect • Goodbye")
    embed.set_thumbnail(url=member.display_avatar.url)
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        pass


@bot.tree.command(name="setgoodbye", description="Canal de despedidas (sin canal = desactivar).")
@app_commands.describe(canal="Canal donde se enviarán las despedidas")
@app_commands.checks.has_permissions(manage_guild=True)
async def setgoodbye_slash(interaction: discord.Interaction, canal: discord.TextChannel = None):
    await interaction.response.send_message(embed=do_setgoodbye(interaction.guild, canal), ephemeral=True)


@bot.command(name="setgoodbye", aliases=["despedida"])
@commands.has_permissions(manage_guild=True)
async def p_setgoodbye(ctx, canal: discord.TextChannel = None):
    await say(ctx, embed=do_setgoodbye(ctx.guild, canal))


# =====================================================================
# SALAS DE VOZ TEMPORALES
# =====================================================================
async def do_setup_tempvoice(guild):
    lobby_id = gsetting(guild, "tempvoice_lobby")
    if lobby_id and guild.get_channel(int(lobby_id)):
        return styled("ℹ️ Ya configurado", f"Ya existe el lobby: {guild.get_channel(int(lobby_id)).mention}", "warn")
    try:
        category = await guild.create_category("🔊 Salas Temporales")
        lobby = await guild.create_voice_channel("➕ Crear sala", category=category)
    except discord.HTTPException:
        return styled("❌ Error", "No pude crear los canales (me falta el permiso Gestionar canales).", "err")
    apply_setting(guild, "tempvoice_lobby", lobby.id)
    return styled("✅ Salas temporales listas", f"Entra a {lobby.mention} y se creará una sala solo para ti. Se borra sola al quedar vacía.", "ok")


@bot.listen("on_voice_state_update")
async def tempvoice_listener(member, before, after):
    guild = member.guild
    rooms = DATA["tempvoice"].setdefault(str(guild.id), {})
    lobby_id = gsetting(guild, "tempvoice_lobby")

    if lobby_id and after.channel and after.channel.id == int(lobby_id) and not member.bot:
        now = time.time()
        if now - TEMPVOICE_LAST.get(member.id, 0) >= 8:
            TEMPVOICE_LAST[member.id] = now
            for cid, owner in list(rooms.items()):
                existing = guild.get_channel(int(cid))
                if owner == member.id and existing:
                    try:
                        await member.move_to(existing)
                    except discord.HTTPException:
                        pass
                    return
            try:
                overwrites = {member: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)}
                room = await guild.create_voice_channel(f"🔊 {member.display_name}"[:90], category=after.channel.category,
                                                        overwrites=overwrites, reason="Nexus: sala temporal")
                try:
                    await member.move_to(room)
                except discord.HTTPException:
                    await room.delete(reason="Nexus: el usuario se fue antes de moverlo")
                    return
                rooms[str(room.id)] = member.id
                save_data()
            except discord.HTTPException:
                pass

    if before.channel and str(before.channel.id) in rooms and len(before.channel.members) == 0:
        try:
            await before.channel.delete(reason="Nexus: sala temporal vacía")
        except discord.HTTPException:
            pass
        rooms.pop(str(before.channel.id), None)
        save_data()


async def cleanup_tempvoice():
    changed = False
    for guild in bot.guilds:
        rooms = DATA["tempvoice"].get(str(guild.id), {})
        for cid in list(rooms):
            channel = guild.get_channel(int(cid))
            if channel is None:
                rooms.pop(cid, None)
                changed = True
            elif len(channel.members) == 0:
                try:
                    await channel.delete(reason="Nexus: sala temporal vacía")
                except discord.HTTPException:
                    pass
                rooms.pop(cid, None)
                changed = True
    if changed:
        save_data()


@bot.tree.command(name="setup-tempvoice", description="Crea el lobby de salas de voz temporales.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_tempvoice_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=await do_setup_tempvoice(interaction.guild), ephemeral=True)


@bot.command(name="setup-tempvoice", aliases=["setuptempvoice"])
@commands.has_permissions(administrator=True)
async def p_setup_tempvoice(ctx):
    await say(ctx, embed=await do_setup_tempvoice(ctx.guild))


# =====================================================================
# MODO PÁNICO
# =====================================================================
PANIC_MODULES = ("antinuke", "antiraid", "antiping", "antispam", "antiinvite", "antiroles", "antiscam")


async def do_panic(guild, actor, on=True):
    if on:
        count = await do_lockdown(guild, "MODO PÁNICO")
        RAID_MODE_UNTIL[guild.id] = time.time() + 1800
        cfg = DATA["config"].setdefault(str(guild.id), {})
        for key in PANIC_MODULES:
            cfg[key] = True
        save_data()
        await send_log(guild, "🚨 MODO PÁNICO ACTIVADO",
                       f"🛡️ **Admin:** {actor.mention}\n🔒 **Canales bloqueados:** {count}\n"
                       f"🚨 **Modo raid:** 30 minutos\n✅ **Todos los módulos de protección activados.**",
                       color=discord.Color.dark_red())
        return styled("🚨 MODO PÁNICO ACTIVADO",
                      f"🔒 **{count}** canales bloqueados\n🚨 Modo raid activo por **30 min**\n✅ Todos los módulos de protección encendidos\n\n"
                      "Para volver a la normalidad usa `N!panic off` o `/panic activar:False`.", "err")
    count = await do_unlockdown(guild)
    RAID_MODE_UNTIL.pop(guild.id, None)
    await send_log(guild, "✅ Modo Pánico Desactivado",
                   f"🛡️ **Admin:** {actor.mention}\n🔓 **Canales restaurados:** {count}", color=discord.Color.green())
    return styled("✅ Modo pánico desactivado", f"🔓 **{count}** canales restaurados y modo raid apagado.", "ok")


@bot.tree.command(name="panic", description="Emergencia: lockdown + modo raid + todas las protecciones activas.")
@app_commands.describe(activar="True = activar, False = volver a la normalidad")
@app_commands.checks.has_permissions(administrator=True)
async def panic_slash(interaction: discord.Interaction, activar: bool = True):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=await do_panic(interaction.guild, interaction.user, activar), ephemeral=True)


@bot.command(name="panic", aliases=["panico", "pánico"])
@commands.has_permissions(administrator=True)
async def p_panic(ctx, estado: str = "on"):
    on = estado.lower() not in ("off", "false", "no", "0", "apagar")
    async with ctx.typing():
        result = await do_panic(ctx.guild, ctx.author, on)
    await say(ctx, embed=result)


# =====================================================================
# SERVER STATS (canales de voz con contadores + comando de estadísticas)
# =====================================================================
STATS_ITEMS = {
    "members": ("👥 Miembros", lambda g: g.member_count or len(g.members)),
    "humans": ("🧑 Humanos", lambda g: sum(1 for m in g.members if not m.bot)),
    "bots": ("🤖 Bots", lambda g: sum(1 for m in g.members if m.bot)),
    "channels": ("💬 Canales", lambda g: len(g.text_channels) + len(g.voice_channels)),
    "roles": ("🎭 Roles", lambda g: max(len(g.roles) - 1, 0)),
    "boosts": ("🚀 Boosts", lambda g: g.premium_subscription_count or 0),
}


def stats_name(key, guild):
    label, fn = STATS_ITEMS[key]
    return f"{label}: {fn(guild)}"


async def do_setup_stats(guild):
    cfg = gsetting(guild, "stats")
    if cfg and any(guild.get_channel(cid) for cid in cfg.get("channels", {}).values()):
        return styled("ℹ️ Ya configurado", "Ya hay canales de estadísticas. Usa `remove-stats` si quieres recrearlos.", "warn")
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=False),
        guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True),
    }
    try:
        category = await guild.create_category("📊 Server Stats", overwrites=overwrites)
        try:
            await category.edit(position=0)
        except discord.HTTPException:
            pass
        ids = {}
        for key in STATS_ITEMS:
            channel = await guild.create_voice_channel(stats_name(key, guild), category=category, overwrites=overwrites)
            ids[key] = channel.id
    except discord.HTTPException:
        return styled("❌ Error", "No pude crear los canales (me falta el permiso Gestionar canales).", "err")
    apply_setting(guild, "stats", {"category": category.id, "channels": ids})
    return styled("✅ Server Stats creado", "Los contadores se actualizan solos cada **10 minutos** (límite de Discord para renombrar canales).", "ok")


async def do_remove_stats(guild):
    cfg = gsetting(guild, "stats")
    if not cfg:
        return styled("ℹ️", "No hay canales de estadísticas configurados.", "warn")
    for cid in list(cfg.get("channels", {}).values()) + [cfg.get("category")]:
        channel = guild.get_channel(cid) if cid else None
        if channel:
            try:
                await channel.delete(reason="Nexus: stats eliminados")
            except discord.HTTPException:
                pass
    apply_setting(guild, "stats", None)
    return styled("🗑️ Server Stats eliminado", "Se borraron los canales de estadísticas.", "ok")


async def update_stats(guild):
    cfg = gsetting(guild, "stats")
    if not cfg:
        return
    changed = False
    for key, cid in list(cfg.get("channels", {}).items()):
        channel = guild.get_channel(cid)
        if channel is None:
            cfg["channels"].pop(key, None)
            changed = True
            continue
        new_name = stats_name(key, guild)
        if channel.name != new_name:
            try:
                await channel.edit(name=new_name, reason="Nexus Stats")
            except discord.HTTPException:
                pass
    if changed:
        save_data()


@tasks.loop(minutes=10)
async def stats_loop():
    for guild in list(bot.guilds):
        try:
            await update_stats(guild)
        except Exception as e:
            print(f"Error en stats de {guild.id}: {type(e).__name__}: {e}")


def build_serverstats_embed(g):
    humans = sum(1 for m in g.members if not m.bot)
    bots = sum(1 for m in g.members if m.bot)
    embed = styled(f"📊 Estadísticas de {g.name}", None, "brand", "Nexus Protect • Stats")
    if g.icon:
        embed.set_thumbnail(url=g.icon.url)
    embed.add_field(name="👥 Miembros", value=f"**{g.member_count}** total\n🧑 {humans} • 🤖 {bots}")
    embed.add_field(name="💬 Canales", value=f"📝 {len(g.text_channels)} texto\n🔊 {len(g.voice_channels)} voz\n📁 {len(g.categories)} categorías")
    embed.add_field(name="🎭 Roles / Emojis", value=f"🎭 {max(len(g.roles) - 1, 0)} roles\n😀 {len(g.emojis)} emojis")
    embed.add_field(name="🚀 Boosts", value=f"**{g.premium_subscription_count or 0}** (nivel {g.premium_tier})")
    embed.add_field(name="🔐 Verificación", value=str(g.verification_level).title())
    embed.add_field(name="📅 Creado", value=f"<t:{int(g.created_at.timestamp())}:R>")
    embed.add_field(name="👑 Dueño", value=f"<@{g.owner_id}>")
    return embed


@bot.tree.command(name="setup-stats", description="Crea canales de voz con contadores del servidor.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_stats_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=await do_setup_stats(interaction.guild), ephemeral=True)


@bot.tree.command(name="remove-stats", description="Elimina los canales de estadísticas.")
@app_commands.checks.has_permissions(administrator=True)
async def remove_stats_slash(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=await do_remove_stats(interaction.guild), ephemeral=True)


@bot.tree.command(name="serverstats", description="Muestra las estadísticas del servidor.")
async def serverstats_slash(interaction: discord.Interaction):
    await interaction.response.send_message(embed=build_serverstats_embed(interaction.guild))


@bot.command(name="setup-stats", aliases=["setupstats"])
@commands.has_permissions(administrator=True)
async def p_setup_stats(ctx):
    async with ctx.typing():
        result = await do_setup_stats(ctx.guild)
    await say(ctx, embed=result)


@bot.command(name="remove-stats", aliases=["removestats"])
@commands.has_permissions(administrator=True)
async def p_remove_stats(ctx):
    async with ctx.typing():
        result = await do_remove_stats(ctx.guild)
    await say(ctx, embed=result)


@bot.command(name="serverstats", aliases=["stats", "sstats"])
async def p_serverstats(ctx):
    await ctx.send(embed=build_serverstats_embed(ctx.guild))


# =====================================================================
# ARRANQUE DE TAREAS EN SEGUNDO PLANO
# =====================================================================
async def start_extra_loops():
    for task_loop in (rotate_presence, giveaway_loop, stats_loop, levels_flush_loop):
        if not task_loop.is_running():
            task_loop.start()
    await cleanup_tempvoice()


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
