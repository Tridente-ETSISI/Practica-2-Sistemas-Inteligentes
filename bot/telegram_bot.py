"""
telegram_bot.py

Bot de Telegram para consultar información de películas y cartelera de Madrid.

El usuario puede preguntar en lenguaje natural:
  - "¿Cuál es la nota de Interstellar?"
  - "Dame la sinopsis de El Padrino"
  - "¿Quién dirigió 2001: A Space Odyssey?"
  - "cartelera madrid"
  - "cartelera en Kinépolis"
  - "películas con nota mayor a 7.5"

El LLM local (Ollama) interpreta la pregunta y extrae la intención
y el nombre de la película.

Variables de entorno necesarias:
  TELEGRAM_BOT_TOKEN  → token del bot (de @BotFather)
  OLLAMA_URL          → URL de Ollama (default: http://localhost:11434)
  OLLAMA_MODEL        → modelo a usar (default: qwen2.5-coder:7b)

Instalación:
  pip install python-telegram-bot playwright
  playwright install chromium
"""

import asyncio
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request

# python-telegram-bot v20+
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ── Path para importar módulos propios ────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cartelera"))

from movie_scraper import get_movie_data_fixed, get_movie_info  # noqa: E402
from cartelera_madrid import (                                   # noqa: E402
    DEFAULT_USER_PROFILE,
    apply_user_profile,
    enrich_with_imdb,
    format_telegram_message,
    get_cartelera_madrid,
)

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OLLAMA_URL:         str = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL:       str = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")

# Tamaño de chunk para mensajes largos (límite Telegram: 4096 chars)
TELEGRAM_CHUNK_SIZE: int = 4000


# ──────────────────────────────────────────────────────────────────────────────
# LLM — INTERPRETAR INTENCIÓN
# ──────────────────────────────────────────────────────────────────────────────

INTENT_PROMPT = """Eres un asistente que interpreta preguntas sobre películas.
Analiza la siguiente pregunta del usuario y devuelve SOLO un JSON válido con esta estructura:

{{
  "intencion": "pelicula" | "cartelera" | "desconocido",
  "titulo_pelicula": "nombre exacto de la película o null",
  "campo": "nota" | "votos" | "sinopsis" | "director" | "duracion" | "todo" | null,
  "filtro_cine": "nombre del cine o null",
  "nota_minima": número o null
}}

Reglas:
- Si pregunta por datos de una película concreta → intencion: "pelicula"
- Si pregunta por la cartelera, cines, qué ponen → intencion: "cartelera"
- campo "todo" si quiere toda la información de la película
- Si pregunta "nota", "puntuación", "rating", "valoración" → campo: "nota"
- Si pregunta "sinopsis", "trama", "de qué va" → campo: "sinopsis"
- Si pregunta "director", "quien dirigió" → campo: "director"
- Si pregunta "duración", "cuánto dura", "minutos" → campo: "duracion"
- Si pregunta "votos", "cuánta gente" → campo: "votos"

Pregunta del usuario: {pregunta}

Responde SOLO con el JSON, sin explicaciones."""


def ask_ollama_intent(pregunta: str) -> dict:
    """
    Llama a Ollama para clasificar la intención del usuario.
    Devuelve {"intencion": "desconocido"} si Ollama no responde o el JSON
    es inválido, en lugar de propagar la excepción al handler.
    """
    prompt  = INTENT_PROMPT.format(pregunta=pregunta)
    payload = json.dumps({
        "model":   OLLAMA_MODEL,
        "prompt":  prompt,
        "stream":  False,
        "options": {"temperature": 0.0},
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        logger.error(f"Ollama no disponible ({OLLAMA_URL}): {e}")
        raise RuntimeError(
            f"No pude conectar con el modelo de lenguaje. "
            f"¿Está Ollama en marcha en {OLLAMA_URL}?"
        ) from e

    raw = data.get("response", "{}")
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*",     "", raw)

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        logger.warning(f"Ollama no devolvió JSON válido: {raw!r}")
        return {"intencion": "desconocido"}

    try:
        return json.loads(match.group())
    except json.JSONDecodeError as e:
        logger.warning(f"JSON de Ollama inválido: {e} — raw: {raw!r}")
        return {"intencion": "desconocido"}


# ──────────────────────────────────────────────────────────────────────────────
# FORMATEAR RESPUESTA DE PELÍCULA
# ──────────────────────────────────────────────────────────────────────────────

def _fuente_badge(data: dict) -> str:
    """Badge de fuente de datos, consistente con los valores de _fuente del proyecto."""
    badges = {
        "playwright_scraping": "🌐 _scraping web_",
        "omdb_api":            "📡 _OMDB API_",
        "cache":               "💾 _caché_",
        "ecartelera":          "🌐 _eCartelera_",
        "sensacine":           "🌐 _SensaCine_",
    }
    return badges.get(data.get("_fuente", ""), "")


def _escape_md(text: str) -> str:
    """Escapa caracteres especiales de Markdown estándar de Telegram."""
    return re.sub(r"([*_`\[\]])", r"\\\1", text)


def format_movie_response(data: dict, campo: str | None) -> str:
    """Formatea la respuesta de una consulta de película para Telegram."""
    titulo = _escape_md(data.get("titulo", "Película"))
    badge  = _fuente_badge(data)

    # Convertimos votos de forma segura a entero para el formateo con comas
    raw_votos = data.get("votos")
    try:
        # Si viene como string con comas/puntos de la API, limpiamos y convertimos
        if isinstance(raw_votos, str):
            votos_int = int(raw_votos.replace(",", "").replace(".", ""))
        else:
            votos_int = int(raw_votos)
        votos_formateados = f"{votos_int:,}"
    except (ValueError, TypeError):
        votos_formateados = str(raw_votos) if raw_votos is not None else "No disponible"

    if campo and campo != "todo":
        valor = data.get(campo)
        if valor is None:
            return f"No encontré información de *{campo}* para *{titulo}*."

        etiquetas: dict[str, str] = {
            "nota":     f"⭐ La nota de *{titulo}* en IMDB es *{valor}*",
            "votos":    f"🗳 *{titulo}* tiene *{votos_formateados}* votos en IMDB", # <-- Corregido
            "sinopsis": f"📖 *Sinopsis de {titulo}*:\n_{_escape_md(str(valor))}_",
            "director": f"🎭 *{titulo}* fue dirigida por *{_escape_md(str(valor))}*",
            "duracion": f"⏱ *{titulo}* dura *{valor} minutos*",
        }
        respuesta = etiquetas.get(campo, f"{campo}: {valor}")
        if badge:
            respuesta += f"\n\n{badge}"
        return respuesta

    # Ficha completa
    lines = [f"🎬 *{titulo}*"]
    if data.get("nota"):
        lines.append(f"⭐ Nota IMDB: *{data['nota']}*")
    if data.get("votos"):
        lines.append(f"🗳 Votos: {votos_formateados}") # <-- Corregido
    if data.get("director"):
        lines.append(f"🎭 Director: {_escape_md(data['director'])}")
    if data.get("duracion"):
        lines.append(f"⏱ Duración: {data['duracion']} min")
    if data.get("sinopsis"):
        sin = data["sinopsis"]
        if len(sin) > 300:
            sin = sin[:300] + "..."
        lines.append(f"📖 Sinopsis: _{_escape_md(sin)}_")
    if data.get("url_imdb"):
        lines.append(f"Ver en IMDB: {data['url_imdb']}")
    if badge:
        lines.append(f"\n{badge}")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS DE ENVÍO
# ──────────────────────────────────────────────────────────────────────────────

async def _reply_long(
    update: Update,
    wait_msg,
    texto: str,
) -> None:
    """
    Envía textos largos dividiendo por líneas de forma segura 
    para no romper las etiquetas de Markdown de Telegram.
    """
    lines = texto.split("\n")
    chunks = []
    current_chunk = []
    current_length = 0

    for line in lines:
        # Si una sola línea es más larga que el límite (raro), la metemos sola
        if len(line) + 1 > TELEGRAM_CHUNK_SIZE:
            if current_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = []
                current_length = 0
            chunks.append(line)
            continue

        # Si añadir la línea supera el límite del chunk, cerramos el chunk actual
        if current_length + len(line) + 1 > TELEGRAM_CHUNK_SIZE:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_length = len(line)
        else:
            current_chunk.append(line)
            current_length += len(line) + 1

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    # Si por alguna razón no hay trozos, salimos
    if not chunks:
        return

    # Enviar el primer bloque editando el mensaje de espera
    await wait_msg.edit_text(
        chunks[0],
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )

    # Enviar los bloques restantes como nuevos mensajes
    for chunk in chunks[1:]:
        if chunk.strip(): # Evitamos enviar trozos vacíos
            await update.message.reply_text(
                chunk,
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )


# ──────────────────────────────────────────────────────────────────────────────
# HANDLERS DEL BOT
# ──────────────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = (
        "🎬 *Bot de Películas*\n\n"
        "Puedo ayudarte con:\n"
        "• Información de cualquier película (nota, sinopsis, director, duración, votos)\n"
        "• Cartelera de cine en Madrid\n\n"
        "*Ejemplos:*\n"
        "— ¿Cuál es la nota de Interstellar?\n"
        "— De qué va El Padrino\n"
        "— ¿Quién dirigió 2001: A Space Odyssey?\n"
        "— Dame toda la info de Dune\n"
        "— Cartelera de Madrid\n"
        "— ¿Qué ponen en Kinépolis?\n"
        "— Películas con nota mayor a 7\\.5\n\n"
        "_Powered by IMDB \\+ Ollama \\+ Playwright_"
    )
    await update.message.reply_text(
        texto,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    pregunta = update.message.text.strip()
    chat_id  = update.effective_chat.id
    logger.info(f"[{chat_id}] Pregunta: {pregunta}")

    wait_msg = await update.message.reply_text("🔍 Procesando tu consulta...")

    try:
        # ── Interpretar intención ──────────────────────────────────────────
        intent    = ask_ollama_intent(pregunta)
        intencion = intent.get("intencion", "desconocido")
        logger.info(f"[{chat_id}] Intención: {intent}")

        # ── CONSULTA DE PELÍCULA ───────────────────────────────────────────
        if intencion == "pelicula":
            await _handle_pelicula(update, context, wait_msg, intent)

        # ── CARTELERA ─────────────────────────────────────────────────────
        elif intencion == "cartelera":
            await _handle_cartelera(update, context, wait_msg, intent)

        # ── DESCONOCIDO ───────────────────────────────────────────────────
        else:
            await wait_msg.edit_text(
                "No entendí tu pregunta. Prueba con algo como:\n"
                "• *¿Cuál es la nota de Inception?*\n"
                "• *Cartelera de Madrid*\n"
                "• *¿Qué ponen en Kinépolis?*",
                parse_mode="Markdown",
            )

    except Exception as e:
        logger.error(f"[{chat_id}] Error procesando mensaje: {e}", exc_info=True)
        await wait_msg.edit_text(
            f"❌ Ocurrió un error inesperado.\n\n_{str(e)[:200]}_\n\n"
            "Inténtalo de nuevo en unos segundos.",
            parse_mode="Markdown",
        )


async def _handle_pelicula(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    wait_msg,
    intent: dict,
) -> None:
    """Handler para consultas de película individual."""
    titulo = intent.get("titulo_pelicula")
    campo  = intent.get("campo") or "todo"

    if not titulo:
        await wait_msg.edit_text(
            "No pude identificar el nombre de la película. "
            "¿Puedes repetirlo más claramente?"
        )
        return

    await wait_msg.edit_text(
        f"🎬 Buscando información de *{_escape_md(titulo)}*...",
        parse_mode="Markdown",
    )

    # Intentar primero get_movie_data_fixed; si falla, caer a get_movie_info.
    # Ambos errores se loguean explícitamente.
    data: dict | None = None
    try:
        data = await asyncio.to_thread(get_movie_data_fixed, titulo)
        logger.info(f"get_movie_data_fixed OK para '{titulo}'")
    except Exception as e_fixed:
        logger.warning(f"get_movie_data_fixed falló para '{titulo}': {e_fixed}. Usando fallback.")
        try:
            data = await asyncio.to_thread(get_movie_info, titulo)
            logger.info(f"get_movie_info OK para '{titulo}'")
        except Exception as e_info:
            logger.error(f"get_movie_info también falló para '{titulo}': {e_info}")
            await wait_msg.edit_text(
                f"❌ No pude obtener información de *{_escape_md(titulo)}*.\n\n"
                f"_{_escape_md(str(e_info)[:300])}_",
                parse_mode="Markdown",
            )
            return

    respuesta = format_movie_response(data, campo)
    await wait_msg.edit_text(
        respuesta,
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def _handle_cartelera(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    wait_msg,
    intent: dict,
) -> None:
    """Handler para consultas de cartelera de Madrid."""
    filtro_cine = intent.get("filtro_cine")
    nota_minima = intent.get("nota_minima")

    msg_espera = "📽 Descargando cartelera de Madrid"
    if filtro_cine:
        msg_espera += f" ({_escape_md(filtro_cine)})"
    msg_espera += "\\.\\.\\.\n_Esto puede tardar unos minutos_"
    await wait_msg.edit_text(msg_espera, parse_mode="Markdown")

    peliculas = await asyncio.to_thread(get_cartelera_madrid, filtro_cine=filtro_cine)

    await wait_msg.edit_text(
        f"📽 Consultando IMDB para {len(peliculas)} películas\\.\\.\\.\n"
        "_Esto puede tardar unos minutos_",
        parse_mode="Markdown",
    )

    # enrich_with_imdb y apply_user_profile son síncronos y bloquean;
    # los ejecutamos en un thread para no bloquear el event loop.
    peliculas = await asyncio.to_thread(enrich_with_imdb, peliculas)
    peliculas = apply_user_profile(peliculas, DEFAULT_USER_PROFILE, nota_minima=nota_minima)

    if not peliculas:
        await wait_msg.edit_text(
            "No encontré películas que cumplan los criterios. "
            "Prueba con un filtro menos restrictivo."
        )
        return

    mensaje = format_telegram_message(peliculas, filtro_cine=filtro_cine)
    await _reply_long(update, wait_msg, mensaje)


# ──────────────────────────────────────────────────────────────────────────────
# ARRANCAR EL BOT
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: Define la variable de entorno TELEGRAM_BOT_TOKEN", file=sys.stderr)
        sys.exit(1)

    print("🚀 Iniciando bot de películas...")
    print(f"   Ollama URL   : {OLLAMA_URL}")
    print(f"   Ollama Model : {OLLAMA_MODEL}")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot en marcha. Pulsa Ctrl+C para detener.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()