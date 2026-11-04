import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from dotenv import load_dotenv

# =========================================================
# CONFIGURAÇÃO
# =========================================================

try:
    FUSO_HORARIO = ZoneInfo("Europe/Lisbon")
except ZoneInfoNotFoundError as erro:
    raise RuntimeError(
        "Não foi encontrada a base de fusos horários. "
        "Instala-a com: python -m pip install tzdata"
    ) from erro

COR_EMBED = discord.Color(0x87CEFA)

PROJECT_DIR = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_DIR / ".env")

token = os.getenv("DISCORD_TOKEN")

if not token:
    raise RuntimeError("DISCORD_TOKEN não está configurado!")

IMAGE_PATH = PROJECT_DIR / "img" / "StaffFlow-Redonda.png"
DATABASE_PATH = Path("/data/staffflow.db")

HORARIOS = [
    "15:00", "16:00", "17:00", "18:00", "19:00",
    "20:00", "21:00", "22:00", "23:00", "00:00+"
]


# =========================================================
# BASE DE DADOS E MIGRAÇÕES
# =========================================================

def iniciar_database():
    with sqlite3.connect(DATABASE_PATH) as db:
        tabelas = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        
        if "configuracao_disponibilidade" in tabelas:
            colunas = [col[1] for col in db.execute("PRAGMA table_info(configuracao_disponibilidade)").fetchall()]
            if "guild_id" not in colunas:
                db.execute("ALTER TABLE configuracao_disponibilidade RENAME TO configuracao_disponibilidade_old")
                tabelas.append("configuracao_disponibilidade_old")

        db.execute("""
            CREATE TABLE IF NOT EXISTS configuracao_disponibilidade (
                guild_id INTEGER PRIMARY KEY,
                canal_id INTEGER NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS paineis_abas_staffflow (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                categoria_id INTEGER,
                prefixo TEXT NOT NULL DEFAULT '',
                mensagem_inicial TEXT
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS ticket_panels (
                panel_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                buttons TEXT NOT NULL
            )
        """)

        colunas_abas = [
            coluna[1]
            for coluna in db.execute("PRAGMA table_info(paineis_abas_staffflow)").fetchall()
        ]

        if "mensagem_inicial" not in colunas_abas:
            db.execute("""
                ALTER TABLE paineis_abas_staffflow
                ADD COLUMN mensagem_inicial TEXT
            """)


def guardar_canal_disponibilidade(guild_id: int, canal_id: int):
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute("""
            INSERT OR REPLACE INTO configuracao_disponibilidade (guild_id, canal_id)
            VALUES (?, ?)
        """, (guild_id, canal_id))


def obter_canal_disponibilidade_id(guild_id: int):
    with sqlite3.connect(DATABASE_PATH) as db:
        resultado = db.execute("""
            SELECT canal_id
            FROM configuracao_disponibilidade
            WHERE guild_id = ?
        """, (guild_id,)).fetchone()

    return resultado[0] if resultado else None


def guardar_painel_abas(
    message_id: int,
    guild_id: int,
    categoria_id: int | None,
    prefixo: str,
    mensagem_inicial: str | None
):
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute("""
            INSERT OR REPLACE INTO paineis_abas_staffflow
            (message_id, guild_id, categoria_id, prefixo, mensagem_inicial)
            VALUES (?, ?, ?, ?, ?)
        """, (
            message_id,
            guild_id,
            categoria_id,
            prefixo,
            mensagem_inicial
        ))


def obter_paineis_abas():
    with sqlite3.connect(DATABASE_PATH) as db:
        return db.execute("""
            SELECT message_id, guild_id, categoria_id, prefixo, mensagem_inicial
            FROM paineis_abas_staffflow
        """).fetchall()


def guardar_ticket_panel(
    panel_id: str, guild_id: int, channel_id: int, message_id: int,
    category_id: int, title: str, description: str, buttons: list
):
    with sqlite3.connect(DATABASE_PATH) as db:
        db.execute("""
            INSERT OR REPLACE INTO ticket_panels (
                panel_id, guild_id, channel_id, message_id,
                category_id, title, description, buttons
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            panel_id, guild_id, channel_id, message_id,
            category_id, title, description,
            json.dumps(buttons, ensure_ascii=False)
        ))


def obter_ticket_panels():
    with sqlite3.connect(DATABASE_PATH) as db:
        return db.execute("""
            SELECT panel_id, guild_id, channel_id, message_id,
                   category_id, title, description, buttons
            FROM ticket_panels
        """).fetchall()


# =========================================================
# FUNÇÕES AUXILIARES
# =========================================================

def criar_embed_abas(texto: str, titulo: str):
    embed = discord.Embed(
        title=titulo,
        description=texto,
        color=COR_EMBED
    )
    embed.set_footer(text="StaffFlow • Sistema de Abas Privadas")
    return embed


def criar_embed_ticket(titulo: str, descricao: str):
    embed = discord.Embed(
        title=titulo,
        description=descricao,
        color=COR_EMBED
    )
    embed.set_footer(text="StaffFlow • Sistema de Tickets")
    return embed


def nome_canal_seguro(texto: str, limite: int = 100) -> str:
    texto = re.sub(r"\s+", "-", texto.strip())
    texto = re.sub(r"-{2,}", "-", texto)
    texto = texto.strip("-")
    return texto[:limite] or "ticket"


async def obter_categoria(guild: discord.Guild, categoria_id: int):
    categoria = guild.get_channel(categoria_id)
    if categoria is None:
        try:
            categoria = await guild.fetch_channel(categoria_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    if isinstance(categoria, discord.CategoryChannel) and categoria.guild.id == guild.id:
        return categoria
    return None


def permissões_canal_privado(guild: discord.Guild, utilizador: discord.Member):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        utilizador: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True
        )
    }

    for cargo in guild.roles:
        if cargo.permissions.administrator:
            overwrites[cargo] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True
            )

    return overwrites


# =========================================================
# DISPONIBILIDADE
# =========================================================

async def obter_canal_votacoes(guild: discord.Guild, canal_id: int | None = None):
    canal_id_final = canal_id or obter_canal_disponibilidade_id(guild.id)

    if canal_id_final is None:
        raise RuntimeError(
            f"O canal de disponibilidade não está configurado para o servidor {guild.name} ({guild.id})."
        )

    canal = guild.get_channel(canal_id_final)

    if canal is None:
        try:
            canal = await guild.fetch_channel(canal_id_final)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            raise RuntimeError(f"O canal ID {canal_id_final} não foi encontrado ou não está acessível no servidor {guild.name}.")

    if not isinstance(canal, discord.TextChannel):
        raise RuntimeError("O ID indicado não corresponde a um canal de texto.")

    return canal


async def publicar_votacoes_reacoes(guild: discord.Guild, canal_id: int | None = None):
    canal = await obter_canal_votacoes(guild, canal_id)
    data = datetime.now(FUSO_HORARIO).strftime("%d/%m/%Y")

    await canal.send(
        f"# {data}\n"
        "Vota conforme a tua disponibilidade."
    )

    for horario in HORARIOS:
        mensagem = await canal.send(f"**🕒 {horario}**")
        await mensagem.add_reaction("☑️")
        await mensagem.add_reaction("❌")


@tasks.loop(time=time(hour=7, minute=0, tzinfo=FUSO_HORARIO))
async def enviar_votacoes_diarias():
    for guild in bot.guilds:
        try:
            canal_id = obter_canal_disponibilidade_id(guild.id)
            if canal_id:
                await publicar_votacoes_reacoes(guild, canal_id)
                print(f"Votações diárias publicadas no servidor: {guild.name} ({guild.id})")
        except Exception as erro:
            print(f"Erro ao publicar votações automáticas no servidor {guild.name} ({guild.id}): {erro}")


@enviar_votacoes_diarias.before_loop
async def antes_das_votacoes():
    await bot.wait_until_ready()


# =========================================================
# CRIAR ABAS
# =========================================================

class CriarAbaView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        categoria_id: int | None = None,
        prefixo: str = "",
        mensagem_inicial: str | None = None
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.categoria_id = categoria_id
        self.prefixo = prefixo or ""
        self.mensagem_inicial = mensagem_inicial

    @discord.ui.button(
        label="Criar Aba",
        style=discord.ButtonStyle.success,
        emoji="📁",
        custom_id="staffflow:criar_aba"
    )
    async def criar_aba(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        guild = interaction.guild
        utilizador = interaction.user

        if guild is None or guild.id != self.guild_id or not isinstance(utilizador, discord.Member):
            await interaction.response.send_message(
                "Este painel não pertence a este servidor ou esta interação é inválida.",
                ephemeral=True
            )
            return

        categoria = None

        if self.categoria_id is not None:
            categoria = await obter_categoria(guild, self.categoria_id)

            if categoria is None:
                await interaction.response.send_message(
                    "A categoria configurada já não existe ou não pertence a este servidor.",
                    ephemeral=True
                )
                return

        canais = categoria.text_channels if categoria else guild.text_channels

        for canal_existente in canais:
            if canal_existente.topic == f"staffflow_owner:{utilizador.id}":
                await interaction.response.send_message(
                    f"Já tens uma aba criada: {canal_existente.mention}",
                    ephemeral=True
                )
                return

        nome = nome_canal_seguro(
            f"{self.prefixo}{utilizador.display_name}",
            100
        ).lower()

        try:
            canal = await guild.create_text_channel(
                name=nome,
                category=categoria,
                overwrites=permissões_canal_privado(guild, utilizador),
                topic=f"staffflow_owner:{utilizador.id}",
                reason=f"Aba privada criada por {utilizador}"
            )

            if self.mensagem_inicial:
                await canal.send(
                    f"{utilizador.mention}\n{self.mensagem_inicial}",
                    allowed_mentions=discord.AllowedMentions(users=True)
                )
            else:
                await canal.send(
                    f"Aba privada criada para {utilizador.mention}.",
                    allowed_mentions=discord.AllowedMentions(users=True)
                )

        except discord.Forbidden:
            await interaction.response.send_message(
                "Não tenho permissões para criar canais ou enviar mensagens.",
                ephemeral=True
            )
            return
        except discord.HTTPException as erro:
            await interaction.response.send_message(
                f"Não foi possível criar a aba: {erro}",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"A tua aba foi criada com sucesso: {canal.mention}",
            ephemeral=True
        )


class MensagemInicialAbaModal(discord.ui.Modal):
    def __init__(self, setup_view):
        super().__init__(title="Mensagem automática da aba")
        self.setup_view = setup_view

        self.mensagem = discord.ui.TextInput(
            label="Mensagem enviada ao abrir uma aba",
            placeholder="Olá! A equipa irá ajudar-te em breve.",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=2000
        )

        self.add_item(self.mensagem)

    async def on_submit(self, interaction: discord.Interaction):
        self.setup_view.mensagem_inicial = str(
            self.mensagem.value
        ).strip()

        await interaction.response.send_message(
            "Mensagem automática configurada com sucesso!",
            ephemeral=True
        )


class CriarAbasSetupView(discord.ui.View):
    def __init__(
        self,
        guild_id: int,
        categoria_id: int | None,
        prefixo: str,
        titulo: str,
        texto: str,
        mensagem_inicial: str | None = None
    ):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.categoria_id = categoria_id
        self.prefixo = prefixo or ""
        self.titulo = titulo
        self.texto = texto
        self.mensagem_inicial = mensagem_inicial

    @discord.ui.button(
        label="Configurar mensagem automática",
        emoji="💬",
        style=discord.ButtonStyle.primary
    )
    async def configurar_mensagem(self, interaction, button):
        await interaction.response.send_modal(
            MensagemInicialAbaModal(self)
        )

    @discord.ui.button(
        label="Publicar painel",
        emoji="📁",
        style=discord.ButtonStyle.success
    )
    async def publicar(self, interaction, button):
        guild = interaction.guild

        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message(
                "Este comando só pode ser utilizado no servidor de origem.",
                ephemeral=True
            )
            return

        if interaction.channel is None:
            await interaction.response.send_message(
                "Não foi possível identificar o canal onde publicar o painel.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        embed = criar_embed_abas(self.texto, self.titulo)

        view = CriarAbaView(
            guild_id=self.guild_id,
            categoria_id=self.categoria_id,
            prefixo=self.prefixo,
            mensagem_inicial=self.mensagem_inicial
        )

        try:
            # Publica apenas a embed e a view, sem anexar imagens
            mensagem = await interaction.channel.send(
                embed=embed,
                view=view
            )

        except discord.Forbidden:
            await interaction.followup.send(
                "Não tenho permissões para publicar o painel neste canal.",
                ephemeral=True
            )
            return
        except discord.HTTPException as erro:
            await interaction.followup.send(
                f"Não foi possível publicar o painel: {erro}",
                ephemeral=True
            )
            return

        guardar_painel_abas(
            mensagem.id,
            self.guild_id,
            self.categoria_id,
            self.prefixo,
            self.mensagem_inicial
        )

        bot.add_view(view, message_id=mensagem.id)

        await interaction.followup.send(
            "Painel de criação de abas publicado com sucesso!",
            ephemeral=True
        )

        self.stop()


# =========================================================
# TICKET TOOL
# =========================================================

class TicketToolView(discord.ui.View):
    def __init__(self, panel_id: str, guild_id: int, category_id: int, buttons: list):
        super().__init__(timeout=None)
        self.panel_id = panel_id
        self.guild_id = guild_id
        self.category_id = category_id
        self.buttons_config = buttons

        for index, config in enumerate(buttons):
            botao = discord.ui.Button(
                label=config["label"],
                emoji=config["emoji"] or None,
                style=discord.ButtonStyle.primary,
                custom_id=f"staffflow:ticket:{panel_id}:{index}"
            )

            async def callback(interaction, button_index=index):
                await self.criar_ticket(interaction, button_index)

            botao.callback = callback
            self.add_item(botao)

    async def criar_ticket(self, interaction: discord.Interaction, button_index: int):
        guild = interaction.guild
        utilizador = interaction.user

        if guild is None or guild.id != self.guild_id or not isinstance(utilizador, discord.Member):
            await interaction.response.send_message(
                "Este botão pertence a outro servidor ou a interação é inválida.",
                ephemeral=True
            )
            return

        config = self.buttons_config[button_index]
        categoria = await obter_categoria(guild, self.category_id)

        if categoria is None:
            await interaction.response.send_message(
                "A categoria configurada já não existe ou não pertence a este servidor.",
                ephemeral=True
            )
            return

        identificador = (
            f"staffflow_ticket:{self.panel_id}:{button_index}:{utilizador.id}"
        )

        for canal_existente in categoria.text_channels:
            if canal_existente.topic == identificador:
                await interaction.response.send_message(
                    f"Já tens um ticket deste tipo aberto: {canal_existente.mention}",
                    ephemeral=True
                )
                return

        emoji = config["emoji"] or "🎫"
        nome = nome_canal_seguro(f"{emoji}{utilizador.display_name}")

        try:
            canal = await guild.create_text_channel(
                name=nome,
                category=categoria,
                overwrites=permissões_canal_privado(guild, utilizador),
                topic=identificador,
                reason=f"Ticket criado por {utilizador}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "Não tenho permissões para criar canais. Verifica a permissão Gerir Canais.",
                ephemeral=True
            )
            return
        except discord.HTTPException as erro:
            print(f"Erro ao criar ticket: {erro}")
            await interaction.response.send_message(
                "Ocorreu um erro ao criar o ticket. Verifica se o nome do canal e o emoji são válidos.",
                ephemeral=True
            )
            return

        cargos_ids = config.get("admin_role_ids", [])
        mencoes_cargos = []

        for cargo_id in cargos_ids:
            cargo = guild.get_role(cargo_id)
            if cargo is not None:
                mencoes_cargos.append(cargo.mention)

        mencoes_cargos_texto = (" " + " ".join(mencoes_cargos)) if mencoes_cargos else ""

        await canal.send(
            f"Obrigado por abrires um ticket, {utilizador.mention}!\n"
            f"A equipa irá responder assim que possível.{mencoes_cargos_texto}",
            allowed_mentions=discord.AllowedMentions(users=True, roles=True)
        )

        await interaction.response.send_message(
            f"O teu ticket foi criado com sucesso: {canal.mention}",
            ephemeral=True
        )


class TicketButtonModal(discord.ui.Modal):
    def __init__(self, setup_view):
        super().__init__(title=f"Configurar botão {len(setup_view.buttons) + 1}")
        self.setup_view = setup_view

        self.label_input = discord.ui.TextInput(
            label="Nome do botão",
            placeholder="Exemplo: Suporte",
            max_length=80,
            required=True
        )
        self.emoji_input = discord.ui.TextInput(
            label="Emoji do botão",
            placeholder="Exemplo: 🎫",
            max_length=100,
            required=False
        )
        self.add_item(self.label_input)
        self.add_item(self.emoji_input)

    async def on_submit(self, interaction: discord.Interaction):
        label = str(self.label_input.value).strip()
        emoji = str(self.emoji_input.value).strip()

        if not label:
            await interaction.response.send_message(
                "O nome do botão não pode estar vazio.",
                ephemeral=True
            )
            return

        if emoji:
            try:
                discord.PartialEmoji.from_str(emoji)
            except (TypeError, ValueError):
                await interaction.response.send_message(
                    "O emoji indicado não é válido.",
                    ephemeral=True
                )
                return

        self.setup_view.buttons.append({
            "label": label,
            "emoji": emoji
        })
        self.setup_view.atualizar_botoes()

        try:
            if self.setup_view.interaction is not None:
                await self.setup_view.interaction.edit_original_response(
                    view=self.setup_view
                )
        except discord.HTTPException as erro:
            print(f"Não foi possível atualizar a configuração do Ticket Tool: {erro}")

        await interaction.response.send_message(
            f"Botão configurado: {emoji} {label}".strip(),
            ephemeral=True
        )


class TicketSetupView(discord.ui.View):
    def __init__(self, guild_id: int, category_id: int, title: str, description: str, quantidade: int):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.category_id = category_id
        self.title = title
        self.description = description
        self.quantidade = quantidade
        self.buttons = []
        self.admin_role_ids = []
        self.interaction = None
        self.atualizar_botoes()

    def atualizar_botoes(self):
        self.clear_items()

        seletor = discord.ui.RoleSelect(
            placeholder="Seleciona os cargos a mencionar nos tickets",
            min_values=0,
            max_values=25
        )

        async def selecionar_cargos(interaction: discord.Interaction):
            self.admin_role_ids = [
                cargo.id for cargo in seletor.values
            ]

            nomes = ", ".join(
                cargo.name for cargo in seletor.values
            ) or "Nenhum cargo selecionado"

            await interaction.response.send_message(
                f"Cargos selecionados: **{nomes}**",
                ephemeral=True
            )

        seletor.callback = selecionar_cargos
        self.add_item(seletor)

        if len(self.buttons) < self.quantidade:
            botao = discord.ui.Button(
                label=f"Configurar botão {len(self.buttons) + 1}",
                emoji="⚙️",
                style=discord.ButtonStyle.primary
            )
            botao.callback = self.abrir_modal
            self.add_item(botao)
        else:
            botao = discord.ui.Button(
                label="Publicar Ticket Tool",
                emoji="📨",
                style=discord.ButtonStyle.success
            )
            botao.callback = self.publicar
            self.add_item(botao)

    async def abrir_modal(self, interaction):
        await interaction.response.send_modal(TicketButtonModal(self))

    async def publicar(self, interaction):
        guild = interaction.guild
        if guild is None or guild.id != self.guild_id:
            await interaction.response.send_message(
                "Não foi possível identificar o servidor.",
                ephemeral=True
            )
            return

        if len(self.buttons) != self.quantidade:
            await interaction.response.send_message(
                "Ainda faltam botões por configurar.",
                ephemeral=True
            )
            return

        categoria = await obter_categoria(guild, self.category_id)
        if categoria is None:
            await interaction.response.send_message(
                "A categoria configurada já não existe ou não pertence a este servidor.",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        panel_id = uuid.uuid4().hex[:12]
        embed = criar_embed_ticket(self.title, self.description)

        buttons_config = [
            {
                **botao,
                "admin_role_ids": self.admin_role_ids
            }
            for botao in self.buttons
        ]

        view = TicketToolView(
            panel_id=panel_id,
            guild_id=self.guild_id,
            category_id=self.category_id,
            buttons=buttons_config
        )

        try:
            mensagem = await interaction.channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                "Não tenho permissões para publicar o painel neste canal.",
                ephemeral=True
            )
            return
        except discord.HTTPException as erro:
            await interaction.followup.send(
                f"Não foi possível publicar o painel: {erro}",
                ephemeral=True
            )
            return

        guardar_ticket_panel(
            panel_id=panel_id,
            guild_id=guild.id,
            channel_id=interaction.channel_id,
            message_id=mensagem.id,
            category_id=self.category_id,
            title=self.title,
            description=self.description,
            buttons=buttons_config
        )
        bot.add_view(view, message_id=mensagem.id)

        await interaction.followup.send(
            "Ticket Tool publicado com sucesso!",
            ephemeral=True
        )
        self.stop()


# =========================================================
# BOT — CLASSE PRINCIPAL
# =========================================================

class StaffFlowBot(commands.Bot):
    async def setup_hook(self):
        iniciar_database()

        for (
            message_id,
            guild_id,
            categoria_id,
            prefixo,
            mensagem_inicial
        ) in obter_paineis_abas():

            self.add_view(
                CriarAbaView(
                    guild_id=guild_id,
                    categoria_id=categoria_id,
                    prefixo=prefixo,
                    mensagem_inicial=mensagem_inicial
                ),
                message_id=message_id
            )

        for painel in obter_ticket_panels():
            (
                panel_id, guild_id, channel_id, message_id,
                category_id, title, description, buttons_json
            ) = painel

            try:
                buttons = json.loads(buttons_json)
                self.add_view(
                    TicketToolView(
                        panel_id=panel_id,
                        guild_id=guild_id,
                        category_id=category_id,
                        buttons=buttons
                    ),
                    message_id=message_id
                )
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as erro:
                print(f"Não foi possível restaurar o painel {panel_id}: {erro}")

        await self.tree.sync()

        if not enviar_votacoes_diarias.is_running():
            enviar_votacoes_diarias.start()


intents = discord.Intents.default()
bot = StaffFlowBot(command_prefix="!", intents=intents)


# =========================================================
# COMANDOS SLASH (ORDEM ALFABÉTICA)
# =========================================================

# 1. /ajuda
@bot.tree.command(
    name="ajuda",
    description="Exibe a lista e explicação de todos os comandos de administração do StaffFlow"
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def ajuda(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 Central de Ajuda • StaffFlow",
        description=(
            "Bem-vindo ao painel de administração do **StaffFlow**!\n"
            "Todos os comandos listados abaixo exigem permissões de **Administrador**.\n\n"
            "Consulta abaixo a utilidade e instruções de cada comando:"
        ),
        color=COR_EMBED
    )

    embed.add_field(
        name="1️⃣ `/ajuda`",
        value=(
            "• **Para que serve:** Apresenta este menu explicativo.\n"
            "• **Como usar:** `/ajuda`\n"
            "• **Permissões:** Administrador."
        ),
        inline=False
    )

    embed.add_field(
        name="2️⃣ `/criar_abas`",
        value=(
            "• **Para que serve:** Publica o painel de criação de abas/canais privados para os utilizadores.\n"
            "• **Como usar:** `/criar_abas [texto] [titulo] [categoria_id] [prefixo]`\n"
            "• **Permissões:** Administrador."
        ),
        inline=False
    )

    embed.add_field(
        name="3️⃣ `/disponibilidade`",
        value=(
            "• **Para que serve:** Configura o canal de disponibilidades do servidor e publica as 15 votações diárias.\n Deve ser usado uma primeira vez para configurar o canal, e depois pode ser usado novamente para publicar as votações automaticamente à meia noite.\n"
            "• **Como usar:** `/disponibilidade canal_id:<ID>`\n"
            "• **Permissões:** Administrador."
        ),
        inline=False
    )

    embed.add_field(
        name="4️⃣ `/ticket_tool`",
        value=(
            "• **Para que serve:** Cria um painel interativo de suporte por tickets com botões personalizados e menção de cargos.\n"
            "• **Como usar:** `/ticket_tool titulo:<txt> descricao:<txt> categoria_id:<ID> quantidade:<1-25>`\n"
            "• **Permissões:** Administrador."
        ),
        inline=False
    )

    embed.set_footer(text="StaffFlow • Gestão e Organização Discord")

    ficheiro = None
    if IMAGE_PATH.is_file():
        ficheiro = discord.File(str(IMAGE_PATH), filename="StaffFlow-Redonda.png")
        embed.set_thumbnail(url="attachment://StaffFlow-Redonda.png")

    if ficheiro:
        await interaction.response.send_message(embed=embed, file=ficheiro, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# 2. /criar_abas
@bot.tree.command(
    name="criar_abas",
    description="Publicar um painel para criar abas privadas"
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    texto="Texto da mensagem (opcional)",
    titulo="Título da mensagem (opcional)",
    categoria_id="ID da categoria onde serão criadas as abas (opcional)",
    prefixo="Prefixo a colocar antes do nome do utilizador (opcional)"
)
async def criar_abas(
    interaction: discord.Interaction,
    texto: str | None = None,
    titulo: str | None = None,
    categoria_id: str | None = None,
    prefixo: str | None = None
):
    guild = interaction.guild

    if guild is None:
        await interaction.response.send_message(
            "Este comando só pode ser utilizado num servidor.",
            ephemeral=True
        )
        return

    texto_final = texto or "Clica no botão abaixo para criares a tua aba privada!"
    titulo_final = titulo or "📁 Criar a tua Aba"

    if len(texto_final) > 4096 or len(titulo_final) > 256:
        await interaction.response.send_message(
            "O título não pode exceder 256 caracteres e o texto não pode exceder 4096 caracteres.",
            ephemeral=True
        )
        return

    categoria = None

    if categoria_id:
        try:
            categoria = await obter_categoria(guild, int(categoria_id))
        except ValueError:
            categoria = None

        if categoria is None:
            await interaction.response.send_message(
                "O ID indicado não corresponde a uma categoria válida deste servidor.",
                ephemeral=True
            )
            return

    view = CriarAbasSetupView(
        guild_id=guild.id,
        categoria_id=categoria.id if categoria else None,
        prefixo=prefixo or "",
        titulo=titulo_final,
        texto=texto_final
    )

    embed = criar_embed_abas(
        texto_final,
        titulo_final
    )

    await interaction.response.send_message(
        content=(
            "**Configuração do painel de criação de abas**\n\n"
            "Podes configurar a mensagem automática que será enviada "
            "quando um utilizador abrir uma aba.\n\n"
            "Quando terminares, carrega em **Publicar painel**."
        ),
        embed=embed,
        view=view,
        ephemeral=True
    )


# 3. /disponibilidade
@bot.tree.command(
    name="disponibilidade",
    description="Configurar o canal e publicar as votações"
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    canal_id="ID do canal onde serão publicadas as disponibilidades"
)
async def disponibilidade(
    interaction: discord.Interaction,
    canal_id: str
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Este comando só pode ser utilizado num servidor.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    try:
        canal_id_int = int(canal_id)
        canal = await obter_canal_votacoes(guild, canal_id_int)

        guardar_canal_disponibilidade(guild.id, canal.id)
        await publicar_votacoes_reacoes(guild, canal.id)

        await interaction.followup.send(
            f"As 15 votações foram publicadas em {canal.mention} e a configuração foi guardada com sucesso!",
            ephemeral=True
        )

    except ValueError:
        await interaction.followup.send(
            "O ID do canal tem de ser um número válido.",
            ephemeral=True
        )
    except (
        discord.NotFound,
        discord.Forbidden,
        discord.HTTPException,
        RuntimeError
    ) as erro:
        print(f"Erro ao publicar disponibilidade no servidor {guild.id}: {erro}")
        await interaction.followup.send(
            "Não foi possível publicar as votações. "
            "Confirma o ID do canal e as permissões do bot neste servidor.",
            ephemeral=True
        )


# 4. /ticket_tool
@bot.tree.command(
    name="ticket_tool",
    description="Criar um painel Ticket Tool personalizado"
)
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    titulo="Título da embed",
    descricao="Descrição da embed",
    categoria_id="ID da categoria onde serão criados os tickets",
    quantidade="Número de botões a configurar (1 a 25)"
)
async def ticket_tool(
    interaction: discord.Interaction,
    titulo: str,
    descricao: str,
    categoria_id: str,
    quantidade: app_commands.Range[int, 1, 25]
):
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Este comando só pode ser utilizado num servidor.",
            ephemeral=True
        )
        return

    try:
        categoria_id_int = int(categoria_id)
    except ValueError:
        categoria_id_int = 0

    categoria = await obter_categoria(guild, categoria_id_int)
    if categoria is None:
        await interaction.response.send_message(
            "O ID indicado não corresponde a uma categoria válida deste servidor.",
            ephemeral=True
        )
        return

    if not titulo.strip() or len(titulo) > 256:
        await interaction.response.send_message(
            "O título é obrigatório e não pode exceder 256 caracteres.",
            ephemeral=True
        )
        return

    if len(descricao) > 4096:
        await interaction.response.send_message(
            "A descrição não pode exceder 4096 caracteres.",
            ephemeral=True
        )
        return

    view = TicketSetupView(
        guild_id=guild.id,
        category_id=categoria.id,
        title=titulo.strip(),
        description=descricao,
        quantidade=quantidade
    )
    view.interaction = interaction

    await interaction.response.send_message(
        "**Configuração do Ticket Tool**\n\n"
        f"Categoria: **{categoria.name}**\n"
        f"Quantidade de botões: **{quantidade}**\n\n"
        "Carrega em «Configurar botão» e indica o nome e o emoji de cada botão. "
        "Quando terminares, carrega em «Publicar Ticket Tool».",
        ephemeral=True,
        view=view
    )


# =========================================================
# ERROS DOS COMANDOS
# =========================================================

@bot.tree.error
async def tratar_erro_comando(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError
):
    if isinstance(error, app_commands.MissingPermissions):
        mensagem = "Não tens permissões de administrador para utilizar este comando."
    elif isinstance(error, app_commands.NoPrivateMessage):
        mensagem = "Este comando só pode ser utilizado num servidor."
    else:
        mensagem = "Ocorreu um erro ao executar o comando."
        print(f"Erro num comando Slash: {error}")

    if interaction.response.is_done():
        await interaction.followup.send(mensagem, ephemeral=True)
    else:
        await interaction.response.send_message(mensagem, ephemeral=True)


# =========================================================
# EVENTOS E ARRANQUE
# =========================================================

@bot.event
async def on_ready():
    print(f"StaffFlow ligado como {bot.user}")
    print("Comandos Slash sincronizados.")
    print("Criar Abas, Disponibilidade e Ticket Tool ativos.")


if __name__ == "__main__":
    iniciar_database()
    bot.run(token)