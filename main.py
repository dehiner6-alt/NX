import os
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

# --- MENSAJES MULTILINGÜES PARA TICKETS ---
TICKET_TEXTS = {
    "es": {
        "already_open": "[ERROR] Ya tienes un ticket abierto en {channel}",
        "welcome_title": "🎟️ Ticket Privado de Soporte",
        "welcome_desc": "Hola {user}, bienvenido a tu ticket.\n\nUn **Administrador** te atenderá en breve. Por favor, describe tu caso detalladamente.",
        "closing": "[TICKET] Eliminando canal privado en 5 segundos...",
        "no_perm_close": "[ERROR] Solo los Administradores o el creador del ticket pueden cerrarlo.",
        "created": "[OK] Ticket privado creado en {channel}"
    },
    "en": {
        "already_open": "[ERROR] You already have an open ticket in {channel}",
        "welcome_title": "🎟️ Private Support Ticket",
        "welcome_desc": "Hello {user}, welcome to your ticket.\n\nAn **Administrator** will assist you shortly. Please describe your issue in detail.",
        "closing": "[TICKET] Deleting private channel in 5 seconds...",
        "no_perm_close": "[ERROR] Only Administrators or the ticket creator can close it.",
        "created": "[OK] Private ticket created in {channel}"
    }
}

# --- FUNCIÓN DE LOGS / ANTIRAID ---
async def send_log(guild, title, description, color=discord.Color.red()):
    log_channel = discord.utils.get(guild.text_channels, name="nexus-logs")
    if not log_channel:
        # Crea el canal de seguridad si no existe
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        log_channel = await guild.create_text_channel(name="nexus-logs", overwrites=overwrites)

    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.utcnow())
    embed.set_footer(text="Nexus Protect • Security Engine")
    await log_channel.send(embed=embed)

# --- SELECCIÓN DE IDIOMA AL ENTRAR (ONBOARDING) ---
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

# --- VISTAS DE TICKETS PRIVADOS ---
class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Cerrar / Close", style=discord.ButtonStyle.red, custom_id="close_ticket_btn")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_lang = "en" if any(r.name == "English" for r in interaction.user.roles) else "es"
        txt = TICKET_TEXTS[user_lang]

        if interaction.user.guild_permissions.administrator or interaction.user.name.lower() in interaction.channel.name:
            await interaction.response.send_message(txt["closing"])
            await asyncio.sleep(5)
            await interaction.channel.delete(reason="Ticket cerrado.")
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
        embed.set_footer(text="Nexus Protect • Support")

        await ticket_channel.send(embed=embed, view=CloseTicketView())
        await interaction.response.send_message(txt["created"].format(channel=ticket_channel.mention), ephemeral=True)

# --- BOT PRINCIPAL CON SISTEMA ANTIRAID Y SEGURIDAD ---
class NexusBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketView())
        self.add_view(CloseTicketView())
        self.add_view(LanguageRoleView())
        await self.tree.sync()
        print("Nexus Protect: Slash Commands, Onboarding y AntiRaid listos.")

bot = NexusBot()

@bot.event
async def on_ready():
    print(f"🛡️ Nexus Protect activo como: {bot.user.name}")
    await bot.change_presence(activity=discord.Game(name="/nexus-status | 24/7 AntiRaid"))

@bot.event
async def on_member_join(member):
    # Log AntiRaid: Detección de entrada de usuarios
    await send_log(
        member.guild,
        "🚨 AntiRaid • Nuevo Usuario",
        f"El usuario {member.mention} (`{member.id}`) ha ingresado al servidor.",
        color=discord.Color.blue()
    )
    
    try:
        embed = discord.Embed(
            title="👋 ¡Bienvenido a Nexus Support / Welcome to Nexus Support!",
            description="Selecciona tu idioma para configurar tu experiencia y soporte privado:\n\n"
                        "Select your language below to configure your experience and support:",
            color=discord.Color.blue()
        )
        await member.send(embed=embed, view=LanguageRoleView())
    except Exception:
        pass

# AntiSpam / AntiRaid: Detección de Menciones Masivas y Enlaces Sospechosos
@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    # 1. Bloqueo de Menciones Masivas (@everyone / @here excesivo)
    if len(message.mentions) > 5 or "@everyone" in message.content or "@here" in message.content:
        if not message.author.guild_permissions.administrator:
            await message.delete()
            await send_log(
                message.guild,
                "⚠️ AntiRaid • Spam de Menciones Detectado",
                f"Mensaje eliminado de {message.author.mention} en {message.channel.mention} por abuso de menciones.",
                color=discord.Color.orange()
            )
            return

    # 2. Bloqueo de Enlaces de Discord Spam / Phishing
    if "discord.gg/" in message.content.lower() or "discord.com/invite/" in message.content.lower():
        if not message.author.guild_permissions.administrator:
            await message.delete()
            await send_log(
                message.guild,
                "🛡️ AntiRaid • Invite Eliminada",
                f"Se eliminó un enlace de invitación enviado por {message.author.mention} en {message.channel.mention}.",
                color=discord.Color.red()
            )
            return

    await bot.process_commands(message)

# --- COMANDOS SLASH ---
@bot.tree.command(name="setup-onboarding", description="Despliega el panel para seleccionar idioma en un canal.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_onboarding(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🌐 Selecciona tu Idioma / Select Your Language",
        description="Haz clic en tu idioma para personalizar los canales y el sistema de tickets.\n\n"
                    "Click on your language button below to customize channels and ticket support.",
        color=discord.Color.blue()
    )
    await interaction.channel.send(embed=embed, view=LanguageRoleView())
    await interaction.response.send_message("[OK] Panel de selección de idioma desplegado.", ephemeral=True)

@bot.tree.command(name="setup-panel-tickets", description="Despliega el panel de soporte.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_panel_tickets(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎟️ Panel de Soporte y Apelaciones / Support Panel",
        description="Presiona el botón de abajo para abrir un ticket de soporte privado.\n"
                    "Press the button below to open a private support ticket.",
        color=discord.Color.blue()
    )
    embed.set_footer(text="Nexus Protect • Support")
    await interaction.channel.send(embed=embed, view=TicketView())
    await interaction.response.send_message("[OK] Panel
