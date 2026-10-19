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

# --- BOT PRINCIPAL ---

class NexusBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(TicketView())
        self.add_view(CloseTicketView())
        self.add_view(LanguageRoleView())
        await self.tree.sync()
        print("Nexus Protect: Slash Commands, Onboarding y Tickets listos.")

bot = NexusBot()

@bot.event
async def on_ready():
    print(f"🛡️ Nexus Protect activo como: {bot.user.name}")
    await bot.change_presence(activity=discord.Game(name="/nexus-status | 24/7 Protected"))

@bot.event
async def on_member_join(member):
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
    await interaction.response.send_message("[OK] Panel de tickets desplegado.", ephemeral=True)

@bot.tree.command(name="nexus-status", description="Muestra el estado de Nexus Protect.")
async def nexus_status(interaction: discord.Interaction):
    embed = discord.Embed(title="Nexus Protect • Estado", description="Sistema activo 24/7.", color=discord.Color.green())
    await interaction.response.send_message(embed=embed)

# --- SERVIDOR WEB INTERNO PARA RENDER (PORT BINDING) ---

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
    print(f"🌐 Servidor Web de puerto activo en el puerto {port}")

# --- INICIALIZACIÓN Y ARRANQUE EN PARALELO ---

async def main():
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        raise ValueError("Error: No se encontró 'DISCORD_TOKEN' en las Variables de Entorno de Render.")
    
    # Inicia el servidor web para satisfacer a Render y mantener el puerto abierto
    await start_web_server()
    
    # Inicia el bot de Discord
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
