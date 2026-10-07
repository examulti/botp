"""
Bot de Discord + Pinterest + Gemini, listo para Render.

- Lee UN tablero público de Pinterest (RSS)
- Clasifica cada foto por reglas (tamaño + palabras del pin) y, si quieres, con Gemini
- La manda al canal que le corresponde (sin repetir)
- Vigila el tablero: cada foto nueva se clasifica y se manda sola a su canal
- Comandos slash: /pfp, /stats y /revisar
- Mini servidor web (/ y /health) para mantenerlo despierto en Render.

Variables de entorno (se ponen en Render, NO en el código):
  DISCORD_TOKEN, GEMINI_API_KEY, PINTEREST_USER, PINTEREST_BOARD
  Opcionales: CLASSIFIER (rules = sin IA, auto = reglas + IA, ai = solo IA), AUTO_SEND (true = manda solo; por defecto
  solo manda cuando usas /revisar), GEMINI_MODEL,
  INTERVAL_MINUTES, CHECK_MINUTES,
  MAX_CLASSIFY_PER_CYCLE, SECONDS_BETWEEN_CALLS, MAX_SEND_PER_CYCLE,
  MISTRAL_API_KEY, MISTRAL_MODEL, AI_ORDER (orden de las IA, por defecto mistral,gemini)
"""
import os
import re
import html
import json
import random
import base64
import asyncio
from typing import Optional

import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------- CONFIG (desde variables de entorno) ----------------
TOKEN = os.environ["DISCORD_TOKEN"]
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")  # solo hace falta si CLASSIFIER es auto o ai
MISTRAL_KEY = os.environ.get("MISTRAL_API_KEY", "")  # IA gratis de Mistral (acepta imágenes)
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-small-latest")
AI_ORDER = [x.strip() for x in os.environ.get("AI_ORDER", "mistral,gemini").split(",") if x.strip()]
# sin IA (rules) por defecto; si pones MISTRAL_API_KEY pasa solo a auto (reglas + IA)
CLASSIFIER = os.environ.get("CLASSIFIER", "auto" if MISTRAL_KEY else "rules").strip().lower()  # rules | auto | ai
AUTO_SEND = os.environ.get("AUTO_SEND", "false").strip().lower() in ("1", "true", "yes", "si", "sí")  # envío automático


def _slug(value: str, last: bool) -> str:
    """Acepta 'randoms' o la URL completa y se queda solo con el nombre."""
    value = re.sub(r"^https?://", "", value.strip())
    value = re.sub(r"^(?:[a-z]{2,3}\.|www\.)?pinterest\.[a-z.]+/", "", value)
    parts = [x for x in value.split("?")[0].split("/") if x]
    if not parts:
        return value
    return re.sub(r"\.rss$", "", parts[-1] if last else parts[0])


PINTEREST_USER = _slug(os.environ["PINTEREST_USER"], last=False)  # ej: fr6652558
BOARD_SLUG = _slug(os.environ["PINTEREST_BOARD"], last=True)  # ej: randoms
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
INTERVAL_MINUTES = int(os.environ.get("INTERVAL_MINUTES", "30"))  # foto extra aleatoria por canal
CHECK_MINUTES = int(os.environ.get("CHECK_MINUTES", "2"))  # cada cuánto busca fotos nuevas en el tablero
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

MAX_CLASSIFY_PER_CYCLE = int(os.environ.get("MAX_CLASSIFY_PER_CYCLE", "30"))  # fotos que clasifica por pasada
SECONDS_BETWEEN_CALLS = float(os.environ.get("SECONDS_BETWEEN_CALLS", "1.5" if MISTRAL_KEY else "6"))  # pausa entre llamadas a la IA
MAX_SEND_PER_CYCLE = int(os.environ.get("MAX_SEND_PER_CYCLE", "40"))  # fotos que manda a canales por revisión
MAX_BOARD_PAGES = 30  # tope de páginas al leer el tablero completo
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
state.setdefault("placed", {})  # foto (sin tamaño) -> categoría del canal donde ya salió

URL_RE = re.compile(r"https://i\.pinimg\.com/\S+")
IMG_RE = re.compile(r"https://i\.pinimg\.com/(?:\d{3,4}x|originals)/[^\s\"'<>&)\]]+")
pin_meta: dict[str, dict] = {}  # url -> {"w", "h", "text"} del pin (para clasificar sin IA)
last_report: list[str] = []  # lo que pasó en la última revisión (lo muestra /revisar)


def log(msg: str):
    print(msg)
    last_report.append(msg)
post_lock = asyncio.Lock()  # evita que dos envíos simultáneos elijan la misma foto
classify_lock = asyncio.Lock()  # evita revisar el tablero dos veces a la vez


def save_state():
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def pin_key(url: str) -> str:
    """Identificador de una foto sin importar el tamaño (736x, originals...)."""
    return re.sub(r"/(?:\d+x|originals)/", "/", url)


def pin_embed(pin: str) -> discord.Embed:
    """Foto limpia dentro de un embed: siempre se ve, sin link suelto."""
    embed = discord.Embed(color=0x2B2D31)
    embed.set_image(url=re.sub(r"/\d+x/", "/originals/", pin))  # alta calidad
    return embed


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
async def fetch_pins_rss(session: aiohttp.ClientSession) -> list[str]:
    """Respaldo: lee el RSS público del tablero (solo trae las fotos más recientes). Devuelve URLs (tamaño 736x), de la más nueva a la más vieja."""
    url = f"https://www.pinterest.com/{PINTEREST_USER}/{BOARD_SLUG}.rss"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    async with session.get(url, headers=headers) as r:
        status = r.status
        ctype = r.headers.get("Content-Type", "")
        text = await r.text(errors="replace")

    if status != 200:
        hint = {
            404: "El tablero no existe o es privado. Revisa PINTEREST_USER y PINTEREST_BOARD, y que el tablero sea público.",
            403: "Pinterest está bloqueando la IP de Render.",
            429: "Pinterest está limitando las peticiones desde Render.",
        }.get(status, "")
        log(f"[!] RSS respondió {status} en {url}\n{hint}".strip())
        return []

    if "<item" not in text:
        log(f"[!] La respuesta no es un RSS con fotos ({ctype}). Empieza así: {text[:120]!r}")
        return []

    pins, keys = [], set()
    for item in re.findall(r"<item\b.*?</item>", text, re.S):
        m = IMG_RE.search(item)
        if not m:
            continue
        key = pin_key(m.group(0))
        if key in keys:
            continue
        keys.add(key)
        pin_url = re.sub(r"/(?:\d+x|originals)/", "/736x/", m.group(0))
        pins.append(pin_url)
        txt = html.unescape(html.unescape(item))
        txt = re.sub(r"https?://\S+", " ", re.sub(r"<[^>]+>", " ", txt))
        pin_meta[pin_url] = {"w": None, "h": None, "text": txt.lower()}

    log(f"Tablero: {len(pins)} fotos encontradas ({PINTEREST_USER}/{BOARD_SLUG})")
    return pins


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
board_id_cache: dict[str, str] = {}


async def pinterest_api(session: aiohttp.ClientSession, resource: str, options: dict) -> dict:
    """Llama a la API interna que usa la web de Pinterest (la misma que carga el tablero)."""
    params = {
        "source_url": f"/{PINTEREST_USER}/{BOARD_SLUG}/",
        "data": json.dumps({"options": options, "context": {}}),
    }
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "X-Pinterest-AppState": "active",
    }
    url = f"https://www.pinterest.com/resource/{resource}/get/"
    async with session.get(url, params=params, headers=headers) as r:
        if r.status != 200:
            raise RuntimeError(f"{resource} respondió {r.status}")
        return await r.json(content_type=None)


async def fetch_pins_api(session: aiohttp.ClientSession) -> list[str]:
    """Lee TODAS las fotos del tablero, página por página, de la más nueva a la más vieja."""
    if "id" not in board_id_cache:
        j = await pinterest_api(
            session, "BoardResource",
            {"username": PINTEREST_USER, "slug": BOARD_SLUG, "field_set_key": "detailed"},
        )
        board_id_cache["id"] = j["resource_response"]["data"]["id"]

    pins, keys, bookmark = [], set(), None
    for _ in range(MAX_BOARD_PAGES):
        options = {"board_id": board_id_cache["id"], "page_size": 100, "field_set_key": "react_grid_pin"}
        if bookmark:
            options["bookmarks"] = [bookmark]
        try:
            j = await pinterest_api(session, "BoardFeedResource", options)
        except Exception:
            if pins:  # ya tengo algunas páginas: sigo con lo que llegó
                break
            raise
        rr = j.get("resource_response", {})
        data = rr.get("data") or []
        for pin in data:
            if not isinstance(pin, dict):
                continue
            images = pin.get("images") or {}
            img = ((images.get("736x") or images.get("orig") or {}).get("url")) or ""
            m = IMG_RE.search(img)
            if not m:
                continue
            key = pin_key(m.group(0))
            if key in keys:
                continue
            keys.add(key)
            pin_url = re.sub(r"/(?:\d+x|originals)/", "/736x/", m.group(0))
            pins.append(pin_url)
            w = h = None
            for size in ("736x", "orig"):
                im = images.get(size) or {}
                if im.get("width") and im.get("height"):
                    w, h = im["width"], im["height"]
                    break
            text = " ".join(
                str(pin.get(k) or "")
                for k in ("title", "grid_title", "description", "auto_alt_text", "seo_alt_text")
            )
            pin_meta[pin_url] = {"w": w, "h": h, "text": text.lower()}
        bookmark = rr.get("bookmark") or (j.get("resource", {}).get("options", {}).get("bookmarks") or [None])[0]
        if not data or not bookmark or bookmark == "-end-":
            break
    return pins


async def fetch_pins(session: aiohttp.ClientSession) -> list[str]:
    """Intenta leer el tablero completo; si Pinterest no deja, usa el RSS."""
    try:
        pins = await fetch_pins_api(session)
        if pins:
            log(f"Tablero completo: {len(pins)} fotos ({PINTEREST_USER}/{BOARD_SLUG})")
            return pins
        log("[!] La lectura completa no devolvió fotos, uso el RSS (solo las más recientes)")
    except Exception as e:
        log(f"[!] No pude leer el tablero completo ({e}), uso el RSS (solo las más recientes)")
    return await fetch_pins_rss(session)


KEYWORDS = [  # en orden de prioridad: la primera categoría que coincida gana
    ("banner", [r"banners?", r"headers?"]),
    ("wallpaper", [r"wallpapers?", r"lock ?screen", r"fondo de pantalla", r"backgrounds?"]),
    ("egirl", [r"e-?girls?", r"e-?boys?", r"gamer girl", r"alt girl", r"scene ?(?:kid|girl|queen)"]),
    ("edgy", [r"edgy", r"goth(?:ic)?", r"emo", r"grunge", r"punk", r"dark", r"skulls?", r"horror", r"villain", r"gore", r"blood"]),
    ("anime", [r"anime", r"manga", r"waifu", r"genshin", r"naruto", r"jujutsu", r"chainsaw man", r"demon slayer", r"one piece", r"fanart", r"chibi", r"vtuber", r"ghibli"]),
    ("soft", [r"soft", r"pastel", r"cute", r"kawaii", r"coquette", r"cottagecore", r"dreamy", r"angelcore", r"fairycore", r"pink"]),
    ("pfp", [r"pfps?", r"profile pic(?:ture)?s?", r"avatars?", r"icons?", r"matching"]),
]
KEYWORD_RES = [(c, re.compile(r"\b(?:" + "|".join(w) + r")\b")) for c, w in KEYWORDS]


def classify_rules(pin: str) -> tuple[Optional[str], bool]:
    """
    Clasifica sin IA: primero por las palabras del pin (título, descripción, alt)
    y, si no hay ninguna, por la forma de la imagen.
    Devuelve (categoría, segura). 'segura' es True solo si coincidió una palabra.
    Si no hay palabras ni una forma clara devuelve (None, False): la foto queda sin categoría.
    """
    meta = pin_meta.get(pin, {})
    text = meta.get("text", "")
    for cat, rx in KEYWORD_RES:
        if rx.search(text):
            return cat, True
    w, h = meta.get("w"), meta.get("h")
    if w and h:
        ratio = w / h
        if ratio >= 1.5:
            return "banner", False  # horizontal y ancha
        if ratio <= 0.6:
            return "wallpaper", False  # muy vertical, tipo pantalla de celular
        if 0.8 <= ratio <= 1.25:
            return "pfp", False  # cuadrada, como foto de perfil
    return None, False


async def classify_pin(session: aiohttp.ClientSession, pin: str) -> tuple[Optional[str], bool]:
    """Elige la categoría según CLASSIFIER. Devuelve (categoría, usó_IA)."""
    rule_cat, confident = classify_rules(pin)
    if CLASSIFIER == "rules" or not ai_providers():
        return rule_cat, False
    if CLASSIFIER == "auto" and confident:
        return rule_cat, False
    try:
        cat = await classify(session, pin)
    except RateLimited:
        if CLASSIFIER == "auto" and rule_cat:
            return rule_cat, False  # sin IA disponible: usa la forma de la imagen
        raise
    if cat is None and CLASSIFIER == "auto" and rule_cat:
        return rule_cat, False
    return cat, True


def ai_providers() -> list[str]:
    """IA disponibles (las que tienen llave), en el orden de AI_ORDER."""
    keys = {"mistral": MISTRAL_KEY, "gemini": GEMINI_KEY}
    return [n for n in AI_ORDER if keys.get(n)]


async def ask_gemini(session: aiohttp.ClientSession, data: bytes, mime: str) -> Optional[str]:
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
            hint = {
                400: "Revisa que GEMINI_API_KEY sea válida.",
                401: "GEMINI_API_KEY no es válida.",
                403: "GEMINI_API_KEY no tiene permiso o es inválida.",
                404: f"El modelo '{GEMINI_MODEL}' no existe: cambia GEMINI_MODEL en Render.",
            }.get(r.status, "")
            log(f"[!] Gemini respondió {r.status}. {hint} {(await r.text())[:150]}")
            return None
        j = await r.json()
    try:
        return j["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return None


async def ask_mistral(session: aiohttp.ClientSession, data: bytes, mime: str) -> Optional[str]:
    body = {
        "model": MISTRAL_MODEL,
        "temperature": 0,
        "max_tokens": 10,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"},
                ],
            }
        ],
    }
    headers = {"Authorization": f"Bearer {MISTRAL_KEY}"}
    async with session.post("https://api.mistral.ai/v1/chat/completions", json=body, headers=headers) as r:
        if r.status == 429:
            raise RateLimited()
        if r.status != 200:
            hint = {
                401: "MISTRAL_API_KEY no es válida.",
                400: f"Revisa MISTRAL_MODEL ('{MISTRAL_MODEL}'): tiene que aceptar imágenes.",
                422: f"Revisa MISTRAL_MODEL ('{MISTRAL_MODEL}'): tiene que aceptar imágenes.",
            }.get(r.status, "")
            log(f"[!] Mistral respondió {r.status}. {hint} {(await r.text())[:150]}")
            return None
        j = await r.json()
    try:
        content = j["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return None
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content


async def classify(session: aiohttp.ClientSession, img_url: str) -> Optional[str]:
    """Pregunta a la IA (Mistral y/o Gemini, en el orden de AI_ORDER) a qué categoría pertenece la imagen."""
    async with session.get(img_url) as r:
        if r.status != 200:
            log(f"[!] No pude descargar la foto ({r.status}): {img_url}")
            return None
        data = await r.read()
        mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]

    limited = False
    for name in ai_providers():
        try:
            ask = ask_mistral if name == "mistral" else ask_gemini
            word = await ask(session, data, mime)
        except RateLimited:
            print(f"[!] {name}: límite alcanzado, pruebo con la siguiente IA")
            limited = True
            continue
        if not word:
            continue
        word = re.sub(r"[^a-z ]", "", word.strip().lower()).strip()
        if word in CATEGORY_TO_CHANNEL:
            return word
        for cat in CATEGORY_TO_CHANNEL:
            if cat in word:
                return cat
        return "unknown"
    if limited:
        raise RateLimited()  # todas las IA llegaron a su límite
    return None


progress = {"running": False, "total": 0, "classified": 0, "sent": 0}


def progress_text() -> str:
    head = ""
    if progress["running"]:
        head = f"🔄 En curso: clasificadas {progress['classified']}/{progress['total']} · enviadas {progress['sent']}\n"
    return (head + "\n".join(last_report)).strip() or "Sin novedades."


async def process_new_pins(full: bool = False) -> bool:
    """
    Revisa el tablero: clasifica las fotos nuevas y manda al canal de su categoría
    toda foto del tablero que todavía no haya salido en ningún canal.
    full=True  -> revisa TODAS sin topes (lo usa /revisar).
    full=False -> con topes por pasada (lo usa el envío automático).
    El resultado queda en last_report.
    Devuelve True si quedan fotos pendientes y conviene volver a pasar ya.
    """
    async with classify_lock:
        last_report.clear()
        progress.update(running=True, total=0, classified=0, sent=0)
        try:
            return await _process_new_pins(full)
        finally:
            progress["running"] = False


async def _process_new_pins(full: bool) -> bool:
    async with aiohttp.ClientSession() as session:
        pins = await fetch_pins(session)
        if not pins:
            return False

        # lo que ya está en algún canal manda: esa es su categoría
        for pin in pins:
            placed = state["placed"].get(pin_key(pin))
            if placed:
                state["classified"][pin] = placed

        new = [p for p in pins if p not in state["classified"]]
        progress["total"] = len(new)
        done = fails = unclear = 0
        stopped = False
        limit = len(new) if (full or CLASSIFIER == "rules") else MAX_CLASSIFY_PER_CYCLE
        for pin in reversed(new[:limit]):  # de la más vieja a la más nueva
            try:
                cat, used_ai = await classify_pin(session, pin)
            except RateLimited:
                log("[!] Límite gratis de la IA alcanzado, sigo en la próxima revisión")
                stopped = True
                break
            if cat:
                state["classified"][pin] = cat
                done += 1
                progress["classified"] = done
                if done % 20 == 0:
                    save_state()
            elif used_ai:
                fails += 1
                if fails >= 3:
                    log("[!] Falló la clasificación 3 veces seguidas, paro hasta la próxima revisión")
                    stopped = True
                    break
            else:
                unclear += 1  # sin palabras ni forma clara: no se manda a ningún canal
            if used_ai:
                await asyncio.sleep(SECONDS_BETWEEN_CALLS)
        save_state()
        log(f"Fotos nuevas: {len(new)} · clasificadas ahora: {done}")
        if unclear:
            log(
                f"Sin categoría clara (no se mandaron): {unclear}. "
                "Con CLASSIFIER=auto la IA las clasifica."
            )
        more = len(new) > limit and done > 0 and not stopped

    warned: set = set()
    sent = 0
    per_channel: dict[str, int] = {}
    for pin in reversed(pins):
        if not full and sent >= MAX_SEND_PER_CYCLE:
            break
        cat = state["placed"].get(pin_key(pin)) or state["classified"].get(pin)
        if cat:
            n = await deliver_pin(pin, cat, warned)
            if n:
                sent += n
                progress["sent"] = sent
                per_channel[cat] = per_channel.get(cat, 0) + n
    detail = ", ".join(f"#{CATEGORY_TO_CHANNEL[c]}: {n}" for c, n in sorted(per_channel.items()))
    log(f"Enviadas a canales: {sent}" + (f" ({detail})" if detail else ""))
    return more or (not full and sent >= MAX_SEND_PER_CYCLE)


async def deliver_pin(pin: str, cat: str, warned: set) -> int:
    """Manda la foto al canal de su categoría en cada servidor. Devuelve cuántas veces la mandó."""
    channel_name = CATEGORY_TO_CHANNEL.get(cat)
    if not channel_name:  # categoría "unknown": no hay canal para ella
        return 0
    sent = 0
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name=channel_name)
        if not channel:
            if (guild.id, channel_name) not in warned:
                warned.add((guild.id, channel_name))
                log(f"[!] No existe el canal #{channel_name} en {guild.name}")
            continue
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            if (guild.id, channel_name) not in warned:
                warned.add((guild.id, channel_name))
                log(f"[!] Sin permiso para escribir o incrustar enlaces en #{channel.name}")
            continue
        try:
            if await send_pin(channel, pin):
                sent += 1
        except Exception as e:
            log(f"[!] Error mandando a #{channel.name}: {e}")
    return sent


# ---------------- Envío de fotos sin repetir ----------------
def used_keys(channel: discord.TextChannel) -> set[str]:
    return {pin_key(u) for u in state["seen"].get(str(channel.id), [])}


def mark_sent(channel: discord.TextChannel, key: str):
    """Anota que la foto salió en este canal y a qué categoría pertenece."""
    state["seen"].setdefault(str(channel.id), []).append(key)
    cat = CHANNEL_TO_CATEGORY.get(channel.name)
    if cat:
        state["placed"].setdefault(key, cat)
    save_state()


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
            candidates = [
                u for u in pool
                if pin_key(u) not in used and state["placed"].get(pin_key(u), cat) == cat
            ]
            if not candidates:
                return "exhausted"
        else:
            candidates = pool

        pin = random.choice(candidates)
        await channel.send(embed=pin_embed(pin))

        key = pin_key(pin)
        if key not in used:
            mark_sent(channel, key)
    return "ok"


async def send_pin(channel: discord.TextChannel, pin: str) -> bool:
    """Manda esta foto concreta al canal, solo si todavía no salió ahí."""
    async with post_lock:
        key = pin_key(pin)
        if key in used_keys(channel):
            return False
        placed = state["placed"].get(key)
        if placed and placed != CHANNEL_TO_CATEGORY.get(channel.name):
            return False  # ya salió en otro canal: no se duplica
        await channel.send(embed=pin_embed(pin))
        mark_sent(channel, key)
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
            urls = URL_RE.findall(msg.content)  # mensajes viejos (link suelto)
            for emb in msg.embeds:  # mensajes nuevos (embed con imagen)
                for part in (emb.image, emb.thumbnail):
                    if part and part.url:
                        urls += URL_RE.findall(part.url)
                if emb.url:
                    urls += URL_RE.findall(emb.url)
            for url in urls:
                key = pin_key(url)
                if key not in known:
                    known.add(key)
                    seen.append(key)
                    added += 1
                cat = CHANNEL_TO_CATEGORY.get(channel.name)
                if cat:
                    state["placed"].setdefault(key, cat)
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
        for _ in range(20):  # si quedan fotos por clasificar, sigue sin esperar al próximo turno
            if not await process_new_pins():
                break
            await asyncio.sleep(5)
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
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await interaction.followup.send(
            f"❌ En {channel.mention} me falta permiso para escribir o para 'Insertar enlaces'."
        )

    status = await post_pin(channel, verificar)
    await interaction.followup.send(status_message(status, channel))


@bot.tree.command(name="stats", description="Fotos clasificadas por categoría y cuántas faltan por enviar")
@app_commands.guild_only()
async def stats(interaction: discord.Interaction):
    counts: dict[str, int] = {}
    for c in state["classified"].values():
        counts[c] = counts.get(c, 0) + 1
    if not counts:
        return await interaction.response.send_message("Aún no hay fotos clasificadas. Usa /revisar para ver qué pasa.", ephemeral=True)

    lines = []
    for cat, total in sorted(counts.items()):
        line = f"**{cat}**: {total} fotos"
        ch = discord.utils.get(interaction.guild.text_channels, name=CATEGORY_TO_CHANNEL.get(cat, ""))
        if ch:
            line += f" · sin enviar en {ch.mention}: {unsent_count(ch, cat)}"
        lines.append(line)
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


review_task: Optional[asyncio.Task] = None


async def run_review():
    try:
        await process_new_pins(full=True)
    except Exception as e:
        log(f"[!] Error revisando el tablero: {e}")


@bot.tree.command(name="revisar", description="Revisa TODAS las fotos del tablero y las manda a su canal")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def revisar(interaction: discord.Interaction):
    global review_task
    await interaction.response.defer(ephemeral=True)
    if classify_lock.locked() or (review_task and not review_task.done()):
        return await interaction.followup.send(("⏳ Ya hay una revisión en curso.\n" + progress_text())[:1900])

    review_task = asyncio.create_task(run_review())
    await asyncio.wait({review_task}, timeout=600)  # si tarda más, sigue sola en segundo plano
    if review_task.done():
        await interaction.followup.send(progress_text()[:1900])
    else:
        await interaction.followup.send(
            ("🔄 Sigue revisando en segundo plano. Usa /revisar otra vez para ver cómo va.\n" + progress_text())[:1900]
        )


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
    print(f"Tablero: https://www.pinterest.com/{PINTEREST_USER}/{BOARD_SLUG}.rss")
    print(f"Clasificador: {CLASSIFIER} · IA disponibles: {ai_providers() or 'ninguna'}")
    if ready_done:  # on_ready puede dispararse más de una vez al reconectar
        return
    ready_done = True

    for guild in bot.guilds:
        await sync_guild(guild)
        for channel in guild.text_channels:
            if channel.name in CHANNEL_TO_CATEGORY:
                await load_history(channel)

    if AUTO_SEND:
        watch_board.start()
        auto_post.start()
    else:
        print("Envío automático apagado: las fotos se mandan al usar /revisar")


@bot.event
async def on_guild_join(guild: discord.Guild):
    await sync_guild(guild)


bot.run(TOKEN)
