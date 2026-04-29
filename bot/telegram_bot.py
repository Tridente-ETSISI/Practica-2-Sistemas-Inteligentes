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

El LLM local (Ollama) interpreta la pregunta y extrae la intención y el nombre de la película.

Variables de entorno necesarias:
  TELEGRAM_BOT_TOKEN  → token del bot (de @BotFather)
  OLLAMA_URL          → URL de Ollama (default: http://localhost:11434)
  OLLAMA_MODEL        → modelo a usar (default: qwen2.5-coder:7b)

Instalación:
  pip install python-telegram-bot playwright
  playwright install chromium
"""

import json
import logging
import os
import re
import sys
import urllib.request
import asyncio

# python-telegram-bot v20+
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Ajustar path para importar módulos propios
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cartelera"))

from movie_scraper import get_movie_info, get_movie_data_fixed
from cartelera_madrid import (
    get_cartelera_madrid,
    enrich_with_imdb,
    apply_user_profile,
    format_telegram_message,
    DEFAULT_USER_PROFILE,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")

# ──────────────────────────────────────────────
# LLM – INTERPRETAR INTENCIÓN
# ──────────────────────────────────────────────

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
    """Usa el LLM para interpretar la intención del usuario."""
    prompt = INTENT_PROMPT.format(pregunta=pregunta)
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0}
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read())

    raw = data.get("response", "{}")
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*", "", raw)

    # Buscar JSON en la respuesta
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"intencion": "desconocido"}


# ──────────────────────────────────────────────
# FORMATEAR RESPUESTA DE PELÍCULA
# ──────────────────────────────────────────────

def _fuente_badge(data: dict) -> str:
    """Devuelve un pequeño badge que indica de dónde vienen los datos."""
    fuente = data.get("_fuente", "")
    if fuente == "playwright_scraping":
        return "🌐 _scraping web_"
    if fuente == "omdb_api":
        return "📡 _OMDB API_"
    if fuente == "cache":
        return "💾 _caché_"
    return ""


def format_movie_response(data: dict, campo: str | None) -> str:
    titulo = data.get("titulo", "Película")
    badge = _fuente_badge(data)

    if campo and campo != "todo":
        valor = data.get(campo)
        if valor is None:
            return f"No encontré información de *{campo}* para *{titulo}*."

        etiquetas = {
            "nota": f"⭐ La nota de *{titulo}* en IMDB es *{valor}*",
            "votos": f"🗳 *{titulo}* tiene *{valor:,}* votos en IMDB",
            "sinopsis": f"📖 *Sinopsis de {titulo}*:\n_{valor}_",
            "director": f"🎭 *{titulo}* fue dirigida por *{valor}*",
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
        lines.append(f"🗳 Votos: {data['votos']:,}")
    if data.get("director"):
        lines.append(f"🎭 Director: {data['director']}")
    if data.get("duracion"):
        lines.append(f"⏱ Duración: {data['duracion']} min")
    if data.get("sinopsis"):
        sin = data["sinopsis"]
        if len(sin) > 300:
            sin = sin[:300] + "..."
        lines.append(f"📖 Sinopsis: _{sin}_")
    if data.get("url_imdb"):
        lines.append(f"[Ver en IMDB]({data['url_imdb']})")
    if badge:
        lines.append(f"\n{badge}")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# HANDLERS DEL BOT
# ──────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "— Películas con nota mayor a 7.5\n\n"
        "_Powered by IMDB + Ollama + Playwright_"
    )
    await update.message.reply_text(texto, parse_mode="Markdown", disable_web_page_preview=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pregunta = update.message.text.strip()
    chat_id = update.effective_chat.id
    logger.info(f"[{chat_id}] Pregunta: {pregunta}")

    # Mensaje de espera
    wait_msg = await update.message.reply_text(
        "🔍 Procesando tu consulta...",
        parse_mode="Markdown"
    )

    try:
        # Interpretar intención con LLM
        intent = ask_ollama_intent(pregunta)
        logger.info(f"Intención detectada: {intent}")

        intencion = intent.get("intencion", "desconocido")

        # ── CONSULTA DE PELÍCULA ───────────────────────────────────────────
        if intencion == "pelicula":
            titulo = intent.get("titulo_pelicula")
            campo = intent.get("campo", "todo")

            if not titulo:
                await wait_msg.edit_text(
                    "No pude identificar el nombre de la película. ¿Puedes repetirlo más claramente?"
                )
                return

            await wait_msg.edit_text(f"🎬 Buscando información de *{titulo}*...", parse_mode="Markdown")

            try:
                try:
                    data = await asyncio.to_thread(get_movie_data_fixed, titulo)
                    print('Algo fue mal de lo de Jaime')
                except:
                    data = await asyncio.to_thread(get_movie_info, titulo)
            except Exception as e:
                # Si falla todo (scraping + API), informar claramente
                await wait_msg.edit_text(
                    f"❌ No pude obtener información de *{titulo}*.\n_{str(e)[:500]}_",
                    parse_mode="None"
                )
                return
            respuesta = format_movie_response(data, campo)
            await wait_msg.edit_text(respuesta, parse_mode="Markdown", disable_web_page_preview=True)

        # ── CARTELERA ─────────────────────────────────────────────────────
        elif intencion == "cartelera":
            filtro_cine = intent.get("filtro_cine")
            nota_minima = intent.get("nota_minima")

            msg_espera = "📽 Descargando cartelera de Madrid"
            if filtro_cine:
                msg_espera += f" ({filtro_cine})"
            msg_espera += "...\n_Esto puede tardar unos minutos_"
            await wait_msg.edit_text(msg_espera, parse_mode="Markdown")

            peliculas = await asyncio.to_thread(get_cartelera_madrid, filtro_cine=filtro_cine)
            await wait_msg.edit_text(
                f"📽 Consultando datos IMDB para {len(peliculas)} películas...\n_Esto puede tardar unos minutos_",
                parse_mode="Markdown"
            )
            peliculas = enrich_with_imdb(peliculas)
            peliculas = apply_user_profile(peliculas, DEFAULT_USER_PROFILE, nota_minima=nota_minima)

            if not peliculas:
                await wait_msg.edit_text(
                    "No encontré películas que cumplan los criterios. Prueba con un filtro menos restrictivo."
                )
                return

            mensaje = format_telegram_message(peliculas, filtro_cine=filtro_cine)
            
            # Telegram tiene límite de 4096 chars
            chunk_size = 4000
            chunks = [mensaje[i:i+chunk_size] for i in range(0, len(mensaje), chunk_size)]
            
            await wait_msg.edit_text(chunks[0], parse_mode="Markdown", disable_web_page_preview=True)
            for chunk in chunks[1:]:
                await update.message.reply_text(chunk, parse_mode="Markdown", disable_web_page_preview=True)

        # ── DESCONOCIDO ───────────────────────────────────────────────────
        else:
            await wait_msg.edit_text(
                "No entendí tu pregunta. Prueba con algo como:\n"
                "• *¿Cuál es la nota de Inception?*\n"
                "• *Cartelera de Madrid*\n"
                "• *¿Qué ponen en Kinépolis?*",
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Error procesando mensaje: {e}", exc_info=True)
        await wait_msg.edit_text(
            f"❌ Ocurrió un error: {str(e)[:200]}\n\nInténtalo de nuevo en unos segundos."
        )


# ──────────────────────────────────────────────
# ARRANCAR EL BOT
# ──────────────────────────────────────────────

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: Define la variable de entorno TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    print(f"🚀 Iniciando bot de películas...")
    print(f"   Ollama URL   : {OLLAMA_URL}")
    print(f"   Ollama Model : {OLLAMA_MODEL}")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("✅ Bot en marcha. Pulsa Ctrl+C para detener.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
