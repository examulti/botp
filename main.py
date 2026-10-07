"""
Bot de Discord + Pinterest + Gemini, listo para Render.

- Lee UN tablero público de Pinterest (RSS)
- Gemini (gratis) clasifica cada foto
- La manda al canal que le corresponde (sin repetir)
- Vigila el tablero: cada foto nueva se clasifica y se manda sola a su canal
- Comandos slash: /pfp, /stats y /revisar
- Mini servidor web (/ y /health) para mantenerlo despierto en Render.

Variables de entorno (se ponen en Render, NO en el código):
  DISCORD_TOKEN, GEMINI_API_KEY, PINTEREST_USER, PINTEREST_BOARD
  Opcionales: GEMINI_MODEL, INTERVAL_MINUTES, CHECK_MINUTES
"""
import os
import re
import json
import random
import base64
import asyncio
import xml.etree.ElementTree as ET
from typing import Optional

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------- CONFIG (desde variables de entorno) ----------------
TOKEN = os.environ["DISCORD_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
PINTEREST_USER = os.environ["PINTEREST_USER"]  # solo el usuario, ej: fr6652558
BOARD_SLUG = os.environ["PINTEREST_BOARD"]  # solo el tablero, ej: randoms
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", "30"))  # foto extra aleatoria por canal
CHECK_MINUTES = int(os.environ.get("CHECK_MINUTES", "5"))  # cada cuánto busca fotos nuevas en el tablero
PORT = int(os.environ.get("PORT", "10000"))  # Render lo pone solo

# categoría que decide la IA -> nombre del canal en Discord
CATEGORY_TO_CHANNEL = {
    "pfp": "pfps",
    "egirl": "egirl",
    "soft": "soft",
    "anime": "anime",
    "edgy": "edgy",
    "banner": "banners",
    "wallpaper": "wallpapers",
}
CHANNEL_TO_CATEGORY = {v: k for k, v in CATEGORY_TO_CHANNEL.items()}

MAX_CLASSIFY_PER_CYCLE = 15  # fotos nuevas que clasifica por revisión
SECONDS_BETWEEN_CALLS = 6  # pausa entre llamadas a Gemini (cuida el límite gratis)
HISTORY_LIMIT = 1000  # mensajes del canal que revisa al arrancar para no repetir
STATE_FILE = "state.json"
# ---------------------------------------------------------------------

PROMPT = (
    "Clasifica esta imagen de Pinterest en UNA sola categoría:\n"
    "- pfp: foto de perfil genérica (cuadrada, un personaje o persona, sin estilo marcado)\n"
    "- egirl: estética e-girl (maquillaje llamativo, cabello de dos colores, ropa alt/gamer)\n"
    "- soft: estética suave/dulce (colores pastel, tierno, delicado)\n"
    "- anime: ilustración o personaje de anime/manga\n"
    "- edgy: oscuro, gótico, agresivo o emo\n"
    "- banner: imagen horizontal y ancha, pensada como banner de perfil\n"
    "- wallpaper: fondo de pantalla (paisaje, arte vertical, ambiente)\n"
    "Responde SOLO con la palabra de la categoría, sin nada más."
)

try:
    with open(STATE_FILE) as f:
        state = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    state = {}
state.setdefault("classified", {})  # url -> categoría
state.setdefault("seen", {})  # id de canal -> fotos ya enviadas

URL_RE = re.compile(r"https://i\.pinimg\.com/\S+")
post_lock = asyncio.Lock()  # evita que dos envíos simultáneos elijan la misma foto
classify_lock = asyncio.Lock()  # evita revisar el tablero dos veces a la vez


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def pin_key(url: str) -> str:
    """Identificador de una foto sin importar el tamaño (736x, originals...)."""
    return re.sub(r"/(?:\d+x|originals)/", "/", url)


class RateLimited(Exception):
    pass


# ---------------- Servidor web para mantenerlo despierto ----------------
async def start_web():
    async def home(_request):
        return web.Response(text="Bot activo")

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/health", home)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"Servidor web en el puerto {PORT}")


class PinBot(commands.Bot):
    async def setup_hook(self):
        await start_web()


# Los comandos slash no necesitan intents especiales
bot = PinBot(command_prefix="!", intents=discord.Intents.default())


# ---------------- Pinterest + Gemini ----------------
async def fetch_pins(session: aiohttp.ClientSession) -> list[str]:
    """Lee el RSS público del tablero. Devuelve URLs (tamaño 736x)."""
    url = f"https://www.pinterest.com/{PINTEREST_USER}/{BOARD_SLUG}.rss"
    async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as r:
        if r.status != 200:
            print(f"[!] RSS {url} -> {r.status}")
            return []
        xml = await r.text()

    pins = []
    for item in ET.fromstring(xml).iter("item"):
        desc = item.findtext("description") or ""
        m = re.search(r'src="(https://i\.pinimg\.com/[^"]+)"', desc)
        if m:
            pins.append(re.sub(r"/\d+x/", "/736x/", m.group(1)))
    return pins


async def classify(session: aiohttp.ClientSession, img_url: str) -> str | None:
    """Pregunta a Gemini a qué categoría pertenece la imagen."""
    async with session.get(img_url) as r:
        if r.status != 200:
            return None
        data = await r.read()
        mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]

    body = {
        "contents": [
            {
                "parts": [
                    {"text": PROMPT},
                    {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
                ]
            }
        ]
    }
    api = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    async with session.post(api, json=body, headers={"x-goog-api-key": GEMINI_KEY}) as r:
        if r.status == 429:
            raise RateLimited()
        if r.status != 200:
            print(f"[!] Gemini -> {r.status}: {(await r.text())[:200]}")
            return None
        j = await r.json()

    try:
        word = j["candidates"][0]["content"]["parts"][0]["text"].strip().lower()
    except (KeyError, IndexError):
        return None

    if word in CATEGORY_TO_CHANNEL:
        return word
    for cat in CATEGORY_TO_CHANNEL:
        if cat in word:
            return cat
    return "unknown"


async def process_new_pins() -> int:
    """
    Revisa el tablero: clasifica las fotos nuevas con Gemini y las manda
    al instante al canal de su categoría. Devuelve cuántas procesó.
    """
    done = 0
    async with classify_lock:
        async with aiohttp.ClientSession() as session:
            pins = await fetch_pins(session)
            new = [p for p in pins if p not in state["classified"]]
            for pin in reversed(new[:MAX_CLASSIFY_PER_CYCLE]):  # de la más vieja a la más nueva
                try:
                    cat = await classify(session, pin)
                except RateLimited:
                    print("[!] Límite gratis de Gemini alcanzado, sigo en la próxima revisión")
                    break
                if cat:
                    state["classified"][pin] = cat
                    save_state()
                    await deliver_pin(pin, cat)
                    done += 1
                await asyncio.sleep(SECONDS_BETWEEN_CALLS)
    return done


async def deliver_pin(pin: str, cat: str):
    """Manda una foto recién clasificada al canal de su categoría en cada servidor."""
    channel_name = CATEGORY_TO_CHANNEL.get(cat)
    if not channel_name:  # categoría "unknown": no hay canal para ella
        return
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if not channel:
            continue
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            print(f"[!] Sin permiso para escribir en #{channel.name} ({guild.name})")
            continue
        try:
            if await send_pin(channel, pin):
                print(f"Foto nueva -> #{channel.name} ({guild.name})")
        except Exception as e:
            print(f"[!] Error mandando a #{channel.name}: {e}")


# ---------------- Envío de fotos sin repetir ----------------
def used_keys(channel: discord.TextChannel) -> set[str]:
    return {pin_key(u) for u in state["seen"].get(str(channel.id), [])}


def unsent_count(channel: discord.TextChannel, cat: str) -> int:
    used = used_keys(channel)
    return sum(1 for u, c in state["classified"].items() if c == cat and pin_key(u) not in used)


async def post_pin(channel: discord.TextChannel, verify: bool = True) -> str:
    """
    Manda una foto de la categoría del canal.
    verify=True  -> nunca manda una foto que ya salió en ese canal.
    verify=False -> puede repetir.
    Devuelve: ok | no_category | empty | exhausted
    """
    cat = CHANNEL_TO_CATEGORY.get(channel.name)
    if not cat:
        return "no_category"

    async with post_lock:
        pool = [u for u, c in state["classified"].items() if c == cat]
        if not pool:
            return "empty"

        used = used_keys(channel)
        if verify:
            candidates = [u for u in pool if pin_key(u) not in used]
            if not candidates:
                return "exhausted"
        else:
            candidates = pool

        pin = random.choice(candidates)
        await channel.send(re.sub(r"/\d+x/", "/originals/", pin))  # alta calidad

        key = pin_key(pin)
        if key not in used:
            state["seen"].setdefault(str(channel.id), []).append(key)
            save_state()
    return "ok"


async def send_pin(channel: discord.TextChannel, pin: str) -> bool:
    """Manda esta foto concreta al canal, solo si todavía no salió ahí."""
    async with post_lock:
        key = pin_key(pin)
        if key in used_keys(channel):
            return False
        await channel.send(re.sub(r"/\d+x/", "/originals/", pin))
        state["seen"].setdefault(str(channel.id), []).append(key)
        save_state()
    return True


async def load_history(channel: discord.TextChannel):
    """
    Lee lo que el bot ya mandó en el canal y lo marca como enviado.
    Así no se repiten fotos aunque Render haya borrado state.json.
    """
    seen = state["seen"].setdefault(str(channel.id), [])
    known = {pin_key(u) for u in seen}
    added = 0
    try:
        async for msg in channel.history(limit=HISTORY_LIMIT):
            if msg.author.id != bot.user.id:
                continue
            for url in URL_RE.findall(msg.content):
                key = pin_key(url)
                if key not in known:
                    known.add(key)
                    seen.append(key)
                    added += 1
    except discord.Forbidden:
        print(f"[!] No puedo leer el historial de #{channel.name} (falta 'Leer historial de mensajes')")
        return
    if added:
        save_state()
        print(f"#{channel.name}: {added} fotos ya enviadas recuperadas del historial")


# ---------------- Tarea automática ----------------
@tasks.loop(minutes=CHECK_MINUTES)
async def watch_board():
    """Cada pocos minutos revisa el tablero y manda al instante las fotos nuevas."""
    try:
        await process_new_pins()
    except Exception as e:
        print(f"[!] Error revisando el tablero: {e}")


@tasks.loop(minutes=INTERVAL_MINUTES)
async def auto_post():
    """Además, cada rato manda una foto aún no enviada a cada canal."""
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name in CHANNEL_TO_CATEGORY:
                try:
                    await post_pin(channel, verify=True)
                except Exception as e:
                    print(f"[!] Error en #{channel.name}: {e}")


# ---------------- Comandos slash ----------------
def status_message(status: str, channel: discord.TextChannel) -> str:
    if status == "ok":
        return f"✅ Foto enviada a {channel.mention}."
    if status == "no_category":
        valid = ", ".join(f"#{n}" for n in CHANNEL_TO_CATEGORY)
        return f"❌ {channel.mention} no tiene categoría. Canales válidos: {valid}"
    if status == "empty":
        return "⏳ Todavía no hay fotos clasificadas para esta categoría. Prueba en unos minutos."
    return (
        f"📭 Ya mandé todas las fotos de esta categoría en {channel.mention}. "
        "Usa `verificar: False` si quieres que se repitan."
    )


@bot.tree.command(name="pfp", description="Manda una foto de la categoría de un canal")
@app_commands.describe(
    canal="Canal donde mandarla (si lo dejas vacío, este mismo)",
    verificar="True = no repite fotos ya enviadas. False = puede repetirlas",
)
@app_commands.guild_only()
async def pfp(
    interaction: discord.Interaction,
    canal: Optional[discord.TextChannel] = None,
    verificar: bool = True,
):
    await interaction.response.defer(ephemeral=True)
    channel = canal or interaction.channel
    if not isinstance(channel, discord.TextChannel):
        return await interaction.followup.send("Usa este comando en un canal de texto.")

    perms = channel.permissions_for(channel.guild.me)
    if not (perms.view_channel and perms.send_messages):
        return await interaction.followup.send(f"❌ No tengo permiso para escribir en {channel.mention}.")

    status = await post_pin(channel, verificar)
    await interaction.followup.send(status_message(status, channel))


@bot.tree.command(name="stats", description="Fotos clasificadas por categoría y cuántas faltan por enviar")
@app_commands.guild_only()
async def stats(interaction: discord.Interaction):
    counts: dict[str, int] = {}
    for c in state["classified"].values():
        counts[c] = counts.get(c, 0) + 1
    if not counts:
        return await interaction.response.send_message("Aún no hay fotos clasificadas.", ephemeral=True)

    lines = []
    for cat, total in sorted(counts.items()):
        line = f"**{cat}**: {total} fotos"
        ch = discord.utils.get(interaction.guild.text_channels, name=CATEGORY_TO_CHANNEL.get(cat, ""))
        if ch:
            line += f" · sin enviar en {ch.mention}: {unsent_count(ch, cat)}"
        lines.append(line)
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="revisar", description="Revisa ahora el tablero y manda las fotos nuevas a sus canales")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def revisar(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        n = await process_new_pins()
    except Exception as e:
        return await interaction.followup.send(f"❌ Error revisando el tablero: {e}")
    if n:
        await interaction.followup.send(f"✅ {n} foto(s) nueva(s) procesadas.")
    else:
        await interaction.followup.send("No hay fotos nuevas en el tablero.")


# ---------------- Eventos ----------------
async def sync_guild(guild: discord.Guild):
    """Registra los comandos slash al instante en el servidor."""
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Comandos slash sincronizados en {guild.name}")
    except discord.Forbidden:
        print(f"[!] Sin permiso para registrar comandos en {guild.name}: reinvita el bot con el scope 'applications.commands'")


ready_done = False


@bot.event
async def on_ready():
    global ready_done
    print(f"Conectado como {bot.user}")
    if ready_done:  # on_ready puede dispararse más de una vez al reconectar
        return
    ready_done = True

    for guild in bot.guilds:
        await sync_guild(guild)
        for channel in guild.text_channels:
            if channel.name in CHANNEL_TO_CATEGORY:
                await load_history(channel)

    watch_board.start()
    auto_post.start()


@bot.event
async def on_guild_join(guild: discord.Guild):
    await sync_guild(guild)


bot.run(TOKEN)
