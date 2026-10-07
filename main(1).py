"""
Bot de Discord + Pinterest + Gemini, listo para Render.

- Lee UN tablero público de Pinterest (RSS)
- Gemini (gratis) clasifica cada foto
- La manda al canal que le corresponde
- Incluye un mini servidor web (/ y /health) para que StayPresent / UptimeRobot
  lo mantenga despierto en Render.

Variables de entorno (se ponen en Render, NO en el código):
  DISCORD_TOKEN, GEMINI_API_KEY, PINTEREST_USER, PINTEREST_BOARD
  Opcionales: GEMINI_MODEL, INTERVAL_MINUTES
"""
import os
import re
import json
import random
import base64
import asyncio
import xml.etree.ElementTree as ET

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands, tasks

# ---------------- CONFIG (desde variables de entorno) ----------------
TOKEN = os.environ["DISCORD_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
PINTEREST_USER = os.environ["PINTEREST_USER"]  # pinterest.com/TU_USUARIO
BOARD_SLUG = os.environ["PINTEREST_BOARD"]  # pinterest.com/usuario/TU_TABLERO/
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", "30"))
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

MAX_CLASSIFY_PER_CYCLE = 15  # fotos nuevas que clasifica por ciclo
SECONDS_BETWEEN_CALLS = 6  # pausa entre llamadas a Gemini (cuida el límite gratis)
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
state.setdefault("seen", {})  # id de canal -> urls ya enviadas


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


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


intents = discord.Intents.default()
intents.message_content = True
bot = PinBot(command_prefix="!", intents=intents)


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


async def update_classifications():
    """Clasifica pins nuevos del tablero (unos cuantos por ciclo)."""
    async with aiohttp.ClientSession() as session:
        pins = await fetch_pins(session)
        new = [p for p in pins if p not in state["classified"]]
        for pin in new[:MAX_CLASSIFY_PER_CYCLE]:
            try:
                cat = await classify(session, pin)
            except RateLimited:
                print("[!] Límite gratis de Gemini alcanzado, sigo en el próximo ciclo")
                break
            if cat:
                state["classified"][pin] = cat
                save_state()
            await asyncio.sleep(SECONDS_BETWEEN_CALLS)


async def post_pin(channel: discord.TextChannel):
    cat = CHANNEL_TO_CATEGORY.get(channel.name)
    if not cat:
        return
    pool = [u for u, c in state["classified"].items() if c == cat]
    if not pool:
        return

    used = state["seen"].setdefault(str(channel.id), [])
    fresh = [u for u in pool if u not in used]
    if not fresh:  # ya mandó todas, reiniciar ciclo
        used.clear()
        fresh = pool

    pin = random.choice(fresh)
    used.append(pin)
    save_state()
    await channel.send(re.sub(r"/\d+x/", "/originals/", pin))  # alta calidad


# ---------------- Tareas y comandos ----------------
@tasks.loop(minutes=INTERVAL_MINUTES)
async def auto_post():
    try:
        await update_classifications()
    except Exception as e:
        print(f"[!] Error clasificando: {e}")

    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name in CHANNEL_TO_CATEGORY:
                try:
                    await post_pin(channel)
                except Exception as e:
                    print(f"[!] Error en #{channel.name}: {e}")


@bot.command(name="pfp")
async def pfp(ctx: commands.Context):
    """!pfp -> manda una imagen de la categoría de este canal."""
    if ctx.channel.name not in CHANNEL_TO_CATEGORY:
        return await ctx.send("Este canal no tiene categoría asignada.")
    await post_pin(ctx.channel)


@bot.command(name="stats")
async def stats(ctx: commands.Context):
    """!stats -> cuántas fotos hay clasificadas por categoría."""
    counts: dict[str, int] = {}
    for c in state["classified"].values():
        counts[c] = counts.get(c, 0) + 1
    lines = [f"{c}: {n}" for c, n in sorted(counts.items())] or ["Aún no hay fotos clasificadas."]
    await ctx.send("\n".join(lines))


@bot.event
async def on_ready():
    print(f"Conectado como {bot.user}")
    if not auto_post.is_running():
        auto_post.start()


bot.run(TOKEN)
