import os
import io
import json
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

# --- ESTRUCTURA ANTINUKE ---
CREATE_CHANNEL_COUNTER = {}
DELETE_CHANNEL_COUNTER = {}

# --- MENSAJES MULTILINGÜES PARA TICKETS ---
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

    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.utcnow())
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

# --- VISTAS DE TICKETS PRIVADOS Y TRANSCRIPCIONES ---
class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Cerrar / Close", style=discord.ButtonStyle.red, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_lang = "en" if any(r.name == "English" for r in interaction.user.roles) else "es"
        txt = TICKET_TEXTS[user_lang]

        if interaction.user.guild_permissions.administrator or interaction.user.name.lower() in interaction.channel.name:
            await interaction.response.send_message(txt["closing"])

            messages = []
            async for msg in interaction.channel.history(limit=500, oldest_first=True):
                time_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                messages.append(f"[{time_str}] {msg.author} ({msg.author.id}): {msg.clean_content}")

            transcript_text = f"=== TRANSCRIPCIÓN DE TICKET: {interaction.channel.name} ===\n"
            transcript_text += f"Servidor: {interaction.guild.name}\nCerrado por: {interaction.user}\n"
            transcript_text += f"Fecha: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            transcript_text += "="*50 + "\n\n" + "\n".join(messages)

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
                        timestamp=datetime.datetime.utcnow()
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

        user_lang = "en" if any(r.name == "English" for r in user.roles) else "es"
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

# --- SELECCIÓN DE IDIOMA (ONBOARDING) ---
class LanguageRoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Español 🇪🇸", style=discord.ButtonStyle.primary, custom_id="role_es_btn")
    async def set_es(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        role_es = discord.utils.get(guild.roles, name="Español")
        role_en = discord.utils.get(guild.roles, name="English")

        if not role_es:
            role_es = await guild.create_role(name="Español")

        if role_en and role_en in interaction.user.roles:
            await interaction.user.remove_roles(role_en)

        await interaction.user.add_roles(role_es)
        await interaction.response.send_message("✅ Has seleccionado el idioma **Español**.", ephemeral=True)

    @discord.ui.button(label="English 🇺🇸", style=discord.ButtonStyle.primary, custom_id="role_en_btn")
    async def set_en(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        role_es = discord.utils.get(guild.roles, name="Español")
        role_en = discord.utils.get(guild.roles, name="English")

        if not role_en:
            role_en = await guild.create_role(name="English")

        if role_es and role_es in interaction.user.roles:
            await interaction.user.remove_roles(role_es)

        await interaction.user.add_roles(role_en)
        await interaction.response.send_message("✅ You have selected **English** language.", ephemeral=True)
# --- BOT PRINCIPAL ---
class NexusBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketView())
        self.add_view(CloseTicketView())
        self.add_view(LanguageRoleView())
        await self.tree.sync()
        print("Nexus Protect: Slash Commands, Onboarding, AntiRaid, AntiNuke y Backup listos.")

bot = NexusBot()

@bot.event
async def on_ready():
    print(f"🛡️ Nexus Protect activo como: {bot.user.name}")
    await bot.change_presence(activity=discord.Game(name="/nexus-status | 24/7 Protected"))

# --- SISTEMA ANTINUKE (DETECCIÓN DE CREACIÓN/ELIMINACIÓN MASIVA) ---
@bot.event
async def on_guild_channel_create(channel):
    guild = channel.guild
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_create):
        user = entry.user
        if user == bot.user or user == guild.owner:
            return
        
        now = datetime.datetime.utcnow().timestamp()
        CREATE_CHANNEL_COUNTER.setdefault(user.id, []).append(now)
        CREATE_CHANNEL_COUNTER[user.id] = [t for t in CREATE_CHANNEL_COUNTER[user.id] if now - t < 10]

        if len(CREATE_CHANNEL_COUNTER[user.id]) >= 3:
            await send_log(
                guild,
                "🚨 ANTINUKE • Creación Masiva de Canales",
                f"⚠️ El usuario {user.mention} (`{user.id}`) creó más de 3 canales en menos de 10 segundos.",
                color=discord.Color.dark_red()
            )

@bot.event
async def on_guild_channel_delete(channel):
    guild = channel.guild
    async for entry in guild.audit_logs(limit=1, action=discord.AuditLogAction.channel_delete):
        user = entry.user
        if user == bot.user or user == guild.owner:
            return
        
        now = datetime.datetime.utcnow().timestamp()
        DELETE_CHANNEL_COUNTER.setdefault(user.id, []).append(now)
        DELETE_CHANNEL_COUNTER[user.id] = [t for t in DELETE_CHANNEL_COUNTER[user.id] if now - t < 10]

        if len(DELETE_CHANNEL_COUNTER[user.id]) >= 3:
            await send_log(
                guild,
                "🚨 ANTINUKE • Eliminación Masiva de Canales",
                f"⚠️ El usuario {user.mention} (`{user.id}`) eliminó más de 3 canales en menos de 10 segundos.",
                color=discord.Color.dark_red()
            )

# --- AUDITORÍA Y LOGS EN TIEMPO REAL ---
@bot.event
async def on_member_join(member):
    avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
    await send_log(
        member.guild,
        "📥 Nuevo Miembro",
        f"👤 **Usuario:** {member.mention} (`{member.name}`)\n🆔 **ID:** `{member.id}`\n📅 **Cuenta Creada:** <t:{int(member.created_at.timestamp())}:R>",
        color=discord.Color.green(),
        thumbnail_url=avatar_url
    )
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
    avatar_url = member.avatar.url if member.avatar else member.default_avatar.url
    await send_log(
        member.guild,
        "📤 Miembro Salió",
        f"👤 **Usuario:** {member.mention} (`{member.name}`)\n🆔 **ID:** `{member.id}`",
        color=discord.Color.dark_gray(),
        thumbnail_url=avatar_url
    )

@bot.event
async def on_member_update(before, after):
    # Cambio de Foto de Perfil
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

    # Cambio de Apodo
    if before.nick != after.nick:
        await send_log(
            after.guild,
            "✏️ Apodo Modificado",
            f"👤 **Usuario:** {after.mention}\n📝 **Antes:** `{before.nick or 'Sin apodo'}`\n📌 **Ahora:** `{after.nick or 'Sin apodo'}`",
            color=discord.Color.gold()
        )

    # Cambio de Roles
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
    
    content = message.content if message.content else "*Mensaje con archivos o embeds*"
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
        f"👤 **Autor:** {before.author.mention} (`{before.author.name}`)\n💬 **Canal:** {before.channel.mention}\n\n🛑 **Antes:**\n```{before.content}```\n✅ **Después:**\n```{after.content}```",
        color=discord.Color.orange()
)
            # --- PROTECCIÓN ANTIRAID ---
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    if len(message.mentions) > 5 or "@everyone" in message.content or "@here" in message.content:
        if not message.author.guild_permissions.administrator:
            await message.delete()
            await send_log(
                message.guild,
                "⚠️ AntiRaid • Spam de Menciones",
                f"🚨 Mensaje eliminado de {message.author.mention} en {message.channel.mention} por abuso de menciones.",
                color=discord.Color.orange()
            )
            return

    if "discord.gg/" in message.content.lower() or "discord.com/invite/" in message.content.lower():
        if not message.author.guild_permissions.administrator:
            await message.delete()
            await send_log(
                message.guild,
                "🛡️ AntiRaid • Invitación Bloqueada",
                f"🚨 Se eliminó un enlace de invitación enviado por {message.author.mention} en {message.channel.mention}.",
                color=discord.Color.red()
            )
            return

    await bot.process_commands(message)

# --- COMANDOS DE BACKUP DEL SERVIDOR ---
@bot.tree.command(name="backup-create", description="Crea una copia de seguridad completa del servidor en formato JSON.")
@app_commands.checks.has_permissions(administrator=True)
async def backup_create(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild

    backup_data = {
        "guild_name": guild.name,
        "created_at": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
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

# --- COMANDOS DASHBOARD / MODERACIÓN CON MANEJO DE ERRORES ---
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
        await interaction.followup.send("❌ **[ERROR]** El bot no tiene permisos para gestionar/eliminar mensajes en este canal.", ephemeral=True)

@bot.tree.command(name="kick", description="Expulsa a un usuario del servidor.")
@app_commands.describe(usuario="Usuario a expulsar", razon="Razón de la expulsión")
@app_commands.checks.has_permissions(kick_members=True)
async def kick(interaction: discord.Interaction, usuario: discord.Member, razon: str = "No especificada"):
    await interaction.response.defer(ephemeral=True)
    try:
        await usuario.kick(reason=razon)
        await interaction.followup.send(f"👢 Usuario {usuario.mention} expulsado. Razón: {razon}", ephemeral=True)
        await send_log(interaction.guild, "👢 Kick de Usuario", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {interaction.user.mention}\n📝 **Razón:** {razon}", color=discord.Color.orange())
    except discord.Forbidden:
        await interaction.followup.send("❌ **[ERROR]** No tengo permisos suficientes para expulsar a este usuario (su rol es igual o superior al mío en la jerarquía).", ephemeral=True)

@bot.tree.command(name="ban", description="Banea a un usuario del servidor.")
@app_commands.describe(usuario="Usuario a banear", razon="Razón del baneo")
@app_commands.checks.has_permissions(ban_members=True)
async def ban(interaction: discord.Interaction, usuario: discord.Member, razon: str = "No especificada"):
    await interaction.response.defer(ephemeral=True)
    try:
        await usuario.ban(reason=razon)
        await interaction.followup.send(f"⛔ Usuario {usuario.mention} baneado. Razón: {razon}", ephemeral=True)
        await send_log(interaction.guild, "⛔ Ban de Usuario", f"👤 **Usuario:** {usuario.mention}\n🛡️ **Moderador:** {interaction.user.mention}\n📝 **Razón:** {razon}", color=discord.Color.red())
    except discord.Forbidden:
        await interaction.followup.send("❌ **[ERROR]** No tengo permisos suficientes para banear a este usuario.", ephemeral=True)

@bot.tree.command(name="nexus-status", description="Muestra el estado de Nexus Protect.")
async def nexus_status(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛡️ Nexus Protect • Estado del Sistema",
        description="🟢 **Estado:** En línea y operativo 24/7\n"
                    "📜 **Transcripciones:** Activas (Envío por MD al dueño)\n"
                    "📦 **Backup System:** Activo (`/backup-create`, `/backup-load`)\n"
                    "🚨 **AntiNuke:** Monitoreando creación/eliminación de canales\n"
                    "🕵️ **Auditoría:** Logs en tiempo real activos\n"
                    "⚔️ **AntiRaid:** Protección activa",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed)

# --- SERVIDOR WEB INTERNO PARA RENDER ---
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

# --- INICIALIZACIÓN ---
async def main():
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise ValueError("Error: No se encontró 'DISCORD_TOKEN' en las Variables de Entorno de Render.")
    
    await start_web_server()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
                
