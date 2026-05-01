"""
alexa_lambda.py
Lambda de AWS para el Alexa Skill de películas.

Intents soportados:
  - NotaPeliculaIntent       → "¿cuál es la nota de {pelicula}?"
  - SinopsisPeliculaIntent   → "cuéntame la trama de {pelicula}"
  - DirectorPeliculaIntent   → "¿quién dirigió {pelicula}?"
  - DuracionPeliculaIntent   → "¿cuánto dura {pelicula}?"
  - VotosPeliculaIntent      → "¿cuántos votos tiene {pelicula}?"
  - InfoCompletaIntent       → "dame información de {pelicula}"

Despliegue:
  1. Crear función Lambda en AWS (Python 3.12)
  2. Subir este archivo como lambda_function.py
  3. Añadir layer con: requests, playwright (o usar requests para llamar a tu API propia)
  4. Variables de entorno Lambda:
       MOVIE_API_URL → URL de tu API propia (si la usas) o vacío para usar requests directo
       IMDB_CACHE_TABLE → nombre de tabla DynamoDB para caché (opcional)

NOTA: Para mantener la práctica simple, esta Lambda llama a una API REST
que tú mismo despliegas (ver api_server.py), evitando instalar Playwright en Lambda.
"""

import json
import logging
import os
import re
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# URL de tu API de películas (ejecuta api_server.py en tu máquina/VPS)
MOVIE_API_URL = os.environ.get("MOVIE_API_URL", "http://TU_IP:8080")


# ──────────────────────────────────────────────
# CLIENTE DE LA API
# ──────────────────────────────────────────────

def get_movie_data(titulo: str) -> dict | None:
    """Llama a la API REST propia para obtener datos de la película."""
    encoded = urllib.parse.quote(titulo)
    url = f"{MOVIE_API_URL}/pelicula?titulo={encoded}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.error(f"Error llamando a API: {e}")
        return None


# ──────────────────────────────────────────────
# HELPERS DE RESPUESTA ALEXA
# ──────────────────────────────────────────────

def build_response(speech_text: str, reprompt_text: str = None,
                   title: str = "Películas", end_session: bool = True,
                   card_text: str = None) -> dict:
    """Construye la respuesta en el formato que espera Alexa."""
    response = {
        "version": "1.0",
        "sessionAttributes": {},
        "response": {
            "outputSpeech": {
                "type": "SSML",
                "ssml": f"<speak>{speech_text}</speak>"
            },
            "card": {
                "type": "Simple",
                "title": title,
                "content": card_text or re.sub(r'<[^>]+>', '', speech_text)
            },
            "shouldEndSession": end_session
        }
    }
    if reprompt_text:
        response["response"]["reprompt"] = {
            "outputSpeech": {
                "type": "PlainText",
                "text": reprompt_text
            }
        }
    return response


def pelicula_no_encontrada(titulo: str) -> dict:
    return build_response(
        f"Lo siento, no encontré información sobre la película {titulo}. "
        "¿Puedes repetir el nombre o prueba con otro título?",
        end_session=False,
        reprompt_text="¿De qué película quieres saber?"
    )


# ──────────────────────────────────────────────
# HANDLERS DE INTENTS
# ──────────────────────────────────────────────

def handle_nota(titulo: str) -> dict:
    data = get_movie_data(titulo)
    if not data or data.get("nota") is None:
        return pelicula_no_encontrada(titulo)
    
    nota = data["nota"]
    votos = data.get("votos")
    speech = f"La película {titulo} tiene una nota de <say-as interpret-as='number'>{nota}</say-as> sobre 10 en IMDB"
    if votos:
        speech += f", basada en {votos:,} votos"
    speech += "."
    return build_response(speech, title=f"Nota de {titulo}")


def handle_sinopsis(titulo: str) -> dict:
    data = get_movie_data(titulo)
    if not data or not data.get("sinopsis"):
        return pelicula_no_encontrada(titulo)
    
    sinopsis = data["sinopsis"]
    # Truncar para Alexa (máximo ~600 chars en voz)
    if len(sinopsis) > 500:
        sinopsis = sinopsis[:500] + "..."
    speech = f"La sinopsis de {titulo} es: {sinopsis}"
    return build_response(speech, title=f"Sinopsis de {titulo}", card_text=data["sinopsis"])


def handle_director(titulo: str) -> dict:
    data = get_movie_data(titulo)
    if not data or not data.get("director"):
        return pelicula_no_encontrada(titulo)
    
    director = data["director"]
    speech = f"{titulo} fue dirigida por {director}."
    return build_response(speech, title=f"Director de {titulo}")


def handle_duracion(titulo: str) -> dict:
    data = get_movie_data(titulo)
    if not data or data.get("duracion") is None:
        return pelicula_no_encontrada(titulo)
    
    duracion = data["duracion"]
    horas = duracion // 60
    minutos = duracion % 60
    if horas > 0:
        speech = f"{titulo} dura {horas} hora{'s' if horas > 1 else ''} y {minutos} minutos."
    else:
        speech = f"{titulo} dura {minutos} minutos."
    return build_response(speech, title=f"Duración de {titulo}")


def handle_votos(titulo: str) -> dict:
    data = get_movie_data(titulo)
    if not data or data.get("votos") is None:
        return pelicula_no_encontrada(titulo)
    
    votos = data["votos"]
    speech = f"La película {titulo} tiene <say-as interpret-as='number'>{votos}</say-as> votos en IMDB."
    return build_response(speech, title=f"Votos de {titulo}")


def handle_info_completa(titulo: str) -> dict:
    data = get_movie_data(titulo)
    if not data:
        return pelicula_no_encontrada(titulo)
    
    parts = [f"Aquí tienes información sobre {titulo}."]
    if data.get("director"):
        parts.append(f"Fue dirigida por {data['director']}.")
    if data.get("nota"):
        parts.append(f"Tiene una nota de {data['nota']} en IMDB.")
    if data.get("duracion"):
        d = data["duracion"]
        h, m = d // 60, d % 60
        parts.append(f"Dura {h} horas y {m} minutos." if h > 0 else f"Dura {m} minutos.")
    if data.get("sinopsis"):
        sin = data["sinopsis"][:300] + "..." if len(data["sinopsis"]) > 300 else data["sinopsis"]
        parts.append(f"La sinopsis es: {sin}")
    
    speech = " ".join(parts)
    card = "\n".join([
        f"Título: {titulo}",
        f"Director: {data.get('director', 'N/A')}",
        f"Nota IMDB: {data.get('nota', 'N/A')}",
        f"Votos: {data.get('votos', 'N/A')}",
        f"Duración: {data.get('duracion', 'N/A')} min",
        f"Sinopsis: {data.get('sinopsis', 'N/A')}",
    ])
    return build_response(speech, title=f"Info de {titulo}", card_text=card)


# ──────────────────────────────────────────────
# HANDLER PRINCIPAL
# ──────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    logger.info(f"Event: {json.dumps(event)}")

    request_type = event.get("request", {}).get("type", "")

    # ── Launch Request ─────────────────────────────────────────────────────
    if request_type == "LaunchRequest":
        return build_response(
            "Bienvenido al asistente de películas. "
            "Puedes preguntarme por la nota, sinopsis, director o duración de cualquier película. "
            "Por ejemplo, di: ¿cuál es la nota de Interstellar?",
            reprompt_text="¿De qué película quieres saber?",
            end_session=False
        )

    # ── Intent Request ─────────────────────────────────────────────────────
    elif request_type == "IntentRequest":
        intent_name = event["request"]["intent"]["name"]
        slots = event["request"]["intent"].get("slots", {})
        
        # Extraer nombre de película del slot
        titulo = None
        for slot_name in ["pelicula", "movie", "titulo", "film"]:
            slot = slots.get(slot_name, {})
            titulo = slot.get("value") or slot.get("resolutions", {}).get("resolutionsPerAuthority", [{}])[0].get("values", [{}])[0].get("value", {}).get("name")
            if titulo:
                break
        
        if not titulo:
            return build_response(
                "No entendí el nombre de la película. ¿Puedes repetirlo?",
                reprompt_text="¿De qué película quieres saber?",
                end_session=False
            )

        logger.info(f"Intent: {intent_name}, Película: {titulo}")

        dispatch = {
            "NotaPeliculaIntent": handle_nota,
            "SinopsisPeliculaIntent": handle_sinopsis,
            "DirectorPeliculaIntent": handle_director,
            "DuracionPeliculaIntent": handle_duracion,
            "VotosPeliculaIntent": handle_votos,
            "InfoCompletaIntent": handle_info_completa,
            # Built-in intents que redirigimos
            "AMAZON.HelpIntent": lambda t: build_response(
                "Puedes preguntarme por la nota, sinopsis, director, duración o votos de cualquier película.",
                end_session=False
            ),
        }

        handler = dispatch.get(intent_name)
        if handler:
            return handler(titulo)
        
        return build_response(
            f"No reconocí la pregunta {intent_name}. Intenta preguntar por la nota, sinopsis o director de una película."
        )

    # ── Session Ended ─────────────────────────────────────────────────────
    elif request_type == "SessionEndedRequest":
        return build_response("¡Hasta luego!")

    return build_response("Ha ocurrido un error inesperado.")
