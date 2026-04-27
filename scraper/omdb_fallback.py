"""
omdb_fallback.py
Fallback para obtener datos de películas via OMDB API (omdbapi.com).

OMDB usa los datos de IMDB y devuelve exactamente los mismos campos
que el scraper: nota, votos, sinopsis, director, duracion.

Para obtener una API key gratuita:
  1. Ve a https://www.omdbapi.com/apikey.aspx
  2. Elige el plan gratuito (1000 peticiones/día)
  3. Recibirás la key por email en segundos

Configura la key en .env:
  OMDB_API_KEY=tu_key_aqui
"""

import json
import os
import re
import urllib.parse
import urllib.request


OMDB_API_KEY = os.environ.get("OMDB_API_KEY", "")
OMDB_BASE_URL = "https://www.omdbapi.com/"


class OMDBError(Exception):
    pass


def _omdb_request(params: dict) -> dict:
    """Hace una petición a la OMDB API y devuelve el JSON."""
    if not OMDB_API_KEY:
        raise OMDBError(
            "OMDB_API_KEY no configurada. "
            "Consíguela gratis en https://www.omdbapi.com/apikey.aspx "
            "y añádela al .env"
        )

    params["apikey"] = OMDB_API_KEY
    query = urllib.parse.urlencode(params)
    url = f"{OMDB_BASE_URL}?{query}"

    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    if data.get("Response") == "False":
        raise OMDBError(f"OMDB: {data.get('Error', 'Error desconocido')}")

    return data


def _parse_runtime(runtime_str: str | None) -> int | None:
    """Convierte '148 min' → 148."""
    if not runtime_str or runtime_str == "N/A":
        return None
    match = re.search(r'(\d+)', runtime_str)
    return int(match.group(1)) if match else None


def _parse_rating(rating_str: str | None) -> float | None:
    """Convierte '8.3' → 8.3, maneja 'N/A'."""
    if not rating_str or rating_str == "N/A":
        return None
    try:
        return float(rating_str)
    except ValueError:
        return None


def _parse_votes(votes_str: str | None) -> int | None:
    """Convierte '1,234,567' → 1234567."""
    if not votes_str or votes_str == "N/A":
        return None
    cleaned = re.sub(r'[^\d]', '', votes_str)
    return int(cleaned) if cleaned else None


def _normalize(raw: dict, movie_title: str) -> dict:
    """Convierte la respuesta de OMDB al formato estándar del scraper."""
    # Construir URL de IMDB si tenemos el ID
    imdb_id = raw.get("imdbID", "")
    url_imdb = f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else None

    return {
        "titulo": movie_title,
        "url_imdb": url_imdb,
        "nota": _parse_rating(raw.get("imdbRating")),
        "votos": _parse_votes(raw.get("imdbVotes")),
        "sinopsis": raw.get("Plot") if raw.get("Plot") != "N/A" else None,
        "director": raw.get("Director") if raw.get("Director") != "N/A" else None,
        "duracion": _parse_runtime(raw.get("Runtime")),
        "_fuente": "omdb_api",   # campo interno para saber qué método se usó
    }


def get_movie_info_omdb(movie_title: str) -> dict:
    """
    Busca una película por título en OMDB y devuelve el diccionario
    en el mismo formato que movie_scraper.get_movie_info().

    Primero intenta búsqueda exacta (t=), si falla busca por texto (s=)
    y toma el primer resultado.
    """
    # Búsqueda exacta por título
    try:
        raw = _omdb_request({"t": movie_title, "type": "movie", "plot": "full", "r": "json"})
        return _normalize(raw, movie_title)
    except OMDBError:
        pass

    # Búsqueda por texto (más flexible)
    search_data = _omdb_request({"s": movie_title, "type": "movie", "r": "json"})
    results = search_data.get("Search", [])
    if not results:
        raise OMDBError(f"OMDB: no se encontró '{movie_title}'")

    # Tomar el primer resultado y pedir sus detalles completos
    imdb_id = results[0].get("imdbID")
    raw = _omdb_request({"i": imdb_id, "plot": "full", "r": "json"})
    return _normalize(raw, movie_title)
