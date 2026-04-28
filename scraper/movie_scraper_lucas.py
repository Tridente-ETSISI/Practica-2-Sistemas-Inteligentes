"""
movie_scraper_lucas.py
Uso: python movie_scraper_lucas.py "Nombre de la pelicula" [opciones]

Opciones:
  --campo {nota,votos,sinopsis,director,duracion,genero}  Devuelve solo ese campo
  --json          Salida en formato JSON
  --no-cache      Ignora la cache y fuerza nueva consulta

Flujo:
1. Abre IMDB con Playwright y espera a que el challenge de AWS WAF se resuelva
2. Busca la pelicula y obtiene su URL
3. Navega a la pagina de la pelicula y extrae los datos via JSON-LD
4. Cachea el resultado en cache.json para no repetir peticiones
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ──────────────────────────────────────────────
# CONFIGURACION
# ──────────────────────────────────────────────

CACHE_FILE = os.path.join(os.path.dirname(__file__), "cache.json")

# Tiempo maximo de espera para que cargue una pagina (en ms)
PAGE_TIMEOUT = 30_000

# Segundos que se espera a que el challenge de AWS WAF se resuelva solo
WAF_WAIT_SECONDS = 5

# Dias antes de que una entrada de cache se considere obsoleta
CACHE_TTL_DAYS = 7

# Numero de reintentos automaticos si falla la navegacion
MAX_RETRIES = 2


# ──────────────────────────────────────────────
# CACHE
# ──────────────────────────────────────────────

def load_cache() -> dict:
    """Carga el fichero de cache si existe; si no, devuelve un dict vacio."""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_cache(cache: dict) -> None:
    """Persiste el dict de cache en disco."""
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[AVISO] No se pudo guardar la cache: {e}", file=sys.stderr)


def is_cache_valid(entry: dict) -> bool:
    """
    Comprueba si una entrada de cache sigue siendo valida.
    Las entradas sin timestamp (cache antigua) se consideran validas
    para no romper compatibilidad con cachés existentes.
    """
    cached_at = entry.get("_cached_at")
    if not cached_at:
        return True
    try:
        fecha = datetime.fromisoformat(cached_at)
        return datetime.now() - fecha < timedelta(days=CACHE_TTL_DAYS)
    except (ValueError, TypeError):
        return True


# ──────────────────────────────────────────────
# PARSERS ROBUSTOS
# ──────────────────────────────────────────────

def parse_nota(value) -> float | None:
    """Convierte cualquier representacion de nota a float. Ej: '8,3' -> 8.3"""
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    match = re.search(r"\d+(\.\d+)?", text)
    return float(match.group()) if match else None


def parse_votos(value) -> int | None:
    """
    Convierte votos a int. Soporta:
    - "2,180,000" o "2.180.000"
    - "2.1M" o "2,1M"
    - "218K"
    """
    if value is None:
        return None

    text_raw = str(value).strip().upper()

    # Caso con sufijo M (millones)
    text_for_suffix = text_raw.replace(",", ".")
    match_m = re.search(r"(\d+(?:\.\d+)?)\s*M", text_for_suffix)
    if match_m:
        return int(float(match_m.group(1)) * 1_000_000)

    # Caso con sufijo K (miles)
    match_k = re.search(r"(\d+(?:\.\d+)?)\s*K", text_for_suffix)
    if match_k:
        return int(float(match_k.group(1)) * 1_000)

    # Eliminar separadores de miles y quedarse solo con digitos
    digits_only = re.sub(r"[,.\s]", "", text_raw)
    if digits_only.isdigit():
        return int(digits_only)

    return None


def parse_duracion(value) -> int | None:
    """
    Convierte duracion a minutos. Soporta:
    - "169" o "169 min"
    - "2h 49m" o "2h49m"
    - "PT2H49M" (formato ISO 8601, usado en el JSON-LD de IMDB)
    """
    if value is None:
        return None

    text = str(value).strip().lower()

    # Formato ISO 8601: PT2H49M
    iso_match = re.search(r"pt(?:(\d+)h)?(?:(\d+)m)?", text)
    if iso_match and (iso_match.group(1) or iso_match.group(2)):
        h = int(iso_match.group(1) or 0)
        m = int(iso_match.group(2) or 0)
        return h * 60 + m

    # Formato "Xh Ym"
    if "h" in text:
        match_h = re.search(r"(\d+)\s*h", text)
        match_m = re.search(r"(\d+)\s*m", text)
        h = int(match_h.group(1)) if match_h else 0
        m = int(match_m.group(1)) if match_m else 0
        total = h * 60 + m
        return total if total > 0 else None

    # Numero simple (minutos directamente)
    match = re.search(r"(\d+)", text)
    if match:
        return int(match.group(1))

    return None


def parse_genero(value) -> list[str] | None:
    """
    Convierte el campo genre del JSON-LD a una lista de strings.
    Soporta tanto string simple como lista de strings.
    Ej: "Action" -> ["Action"]  |  ["Action", "Sci-Fi"] -> ["Action", "Sci-Fi"]
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [str(g).strip() for g in value if g]
    return [str(value).strip()] if str(value).strip() else None


# ──────────────────────────────────────────────
# UTILIDAD: navegar superando el WAF
# ──────────────────────────────────────────────

def goto_safe(page, url: str) -> None:
    """
    Navega a una URL y espera a que el challenge de AWS WAF se resuelva.
    El challenge ejecuta JS, obtiene un token y recarga la pagina real.
    Esperamos activamente a que el HTML tenga contenido suficiente.
    """
    try:
        page.goto(url, timeout=PAGE_TIMEOUT, wait_until="domcontentloaded")
    except PlaywrightTimeoutError as e:
        raise RuntimeError(f"Timeout cargando {url}") from e

    # Si recibimos el challenge, esperar a que se resuelva y recargue
    if "awswaf" in page.content() or len(page.content()) < 5000:
        print(f"      [WAF] Challenge detectado, esperando recarga...", file=sys.stderr)
        try:
            # Primero esperar a que exista el body (la recarga lo elimina temporalmente)
            # y luego a que tenga contenido real
            page.wait_for_function(
                "document.body !== null && document.body.innerHTML.length > 50000",
                timeout=20000
            )
        except PlaywrightTimeoutError:
            # Fallback: esperar networkidle
            try:
                page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT)
            except PlaywrightTimeoutError:
                pass


# ──────────────────────────────────────────────
# PASO 1 - Obtener URL de IMDB para la pelicula
# ──────────────────────────────────────────────

def get_imdb_url(page, movie_title: str) -> str:
    """
    Busca la pelicula en IMDB usando Playwright y devuelve la URL de su pagina.
    Recibe el objeto 'page' ya abierto para reutilizar el navegador.
    """
    query = urllib.parse.quote_plus(movie_title)
    search_url = f"https://www.imdb.com/find/?q={query}&s=tt&ttype=ft"

    goto_safe(page, search_url)

    try:
        page.wait_for_selector('a[href*="/title/tt"]', timeout=10000)
    except PlaywrightTimeoutError:
        raise ValueError(f"No se encontraron enlaces de peliculas para '{movie_title}'")

    links = page.locator('a[href*="/title/tt"]').evaluate_all(
        "els => els.map(a => a.getAttribute('href'))"
    )

    urls = []
    for href in links:
        if not href:
            continue
        match = re.search(r"/title/(tt\d+)/?", href)
        if match:
            url = f"https://www.imdb.com/title/{match.group(1)}/"
            if url not in urls:
                urls.append(url)

    if not urls:
        print("[DEBUG] Links detectados:", links[:10], file=sys.stderr)
        raise ValueError(f"No se encontro '{movie_title}' en IMDB")

    return urls[0]


# ──────────────────────────────────────────────
# PASO 2 - Extraer datos de la pagina de la pelicula
# ──────────────────────────────────────────────

def extract_movie_data(page, url: str) -> dict:
    """
    Navega a la pagina de la pelicula y extrae los datos.

    Estrategia principal: leer el bloque JSON-LD que IMDB incluye en todas
    sus paginas. Contiene nota, votos, director, duracion, genero y sinopsis
    de forma estructurada, sin depender de selectores CSS que pueden cambiar.

    Estrategia de respaldo: selectores CSS directos sobre el DOM renderizado.
    """
    goto_safe(page, url)

    resultado = {
        "nota":     None,
        "votos":    None,
        "sinopsis": None,
        "director": None,
        "duracion": None,
        "genero":   None,
    }

    # ── Estrategia 1: JSON-LD (mas fiable) ──────────────────────────────────
    try:
        ld_content = page.locator('script[type="application/ld+json"]').first.inner_text(timeout=5000)
        ld = json.loads(ld_content)

        aggregate = ld.get("aggregateRating", {})
        resultado["nota"]  = parse_nota(aggregate.get("ratingValue"))
        resultado["votos"] = parse_votos(aggregate.get("ratingCount"))

        director_ld = ld.get("director")
        if isinstance(director_ld, list) and director_ld:
            resultado["director"] = director_ld[0].get("name")
        elif isinstance(director_ld, dict):
            resultado["director"] = director_ld.get("name")

        resultado["duracion"] = parse_duracion(ld.get("duration"))
        resultado["sinopsis"] = ld.get("description") or None
        resultado["genero"]   = parse_genero(ld.get("genre"))

    except (json.JSONDecodeError, PlaywrightTimeoutError) as e:
        # Solo capturamos errores esperados, no bugs de programacion
        print(f"      [AVISO] JSON-LD no disponible: {e}", file=sys.stderr)

    # ── Estrategia 2: selectores CSS (respaldo para campos que falten) ───────

    if resultado["nota"] is None:
        try:
            el = page.locator('[data-testid="hero-rating-bar__aggregate-rating__score"] span').first
            resultado["nota"] = parse_nota(el.inner_text(timeout=5000))
        except PlaywrightTimeoutError:
            pass

    if resultado["votos"] is None:
        try:
            el = page.locator('[data-testid="hero-rating-bar__aggregate-rating__score"]').first
            parent_text = el.locator("..").inner_text(timeout=5000)
            v_match = re.search(r"([\d,.]+[KkMm]?)\s*(?:votos|votes|ratings?)", parent_text, re.IGNORECASE)
            if v_match:
                resultado["votos"] = parse_votos(v_match.group(1))
        except PlaywrightTimeoutError:
            pass

    if resultado["sinopsis"] is None:
        for selector in ['[data-testid="plot-xl"]', '[data-testid="plot-l"]', '[data-testid="plot"]']:
            try:
                texto = page.locator(selector).first.inner_text(timeout=3000).strip()
                if texto:
                    resultado["sinopsis"] = texto
                    break
            except PlaywrightTimeoutError:
                continue

    if resultado["director"] is None:
        try:
            el = page.locator('[data-testid="title-pc-principal-credit"] a').first
            resultado["director"] = el.inner_text(timeout=3000).strip() or None
        except PlaywrightTimeoutError:
            pass

    if resultado["duracion"] is None:
        try:
            el = page.locator('[data-testid="title-techspec_runtime"]').first
            resultado["duracion"] = parse_duracion(el.inner_text(timeout=3000))
        except PlaywrightTimeoutError:
            pass

    return resultado


# ──────────────────────────────────────────────
# FUNCION PRINCIPAL
# ──────────────────────────────────────────────

def get_movie_info(movie_title: str, use_cache: bool = True) -> dict:
    """
    Devuelve un diccionario con: titulo, url_imdb, nota, votos,
    sinopsis, director, duracion, genero.
    Usa cache con TTL para evitar peticiones innecesarias.
    Reintenta automaticamente hasta MAX_RETRIES veces si hay un error.
    """
    movie_title = movie_title.strip()
    if not movie_title:
        raise ValueError("El titulo de la pelicula no puede estar vacio")

    cache = load_cache()
    cache_key = movie_title.lower()

    # Comprobar cache (con validacion de TTL)
    if use_cache and cache_key in cache:
        entry = cache[cache_key]
        if is_cache_valid(entry):
            print(f"[CACHE] '{movie_title}' obtenido de cache local.", file=sys.stderr)
            return entry
        else:
            print(f"[CACHE] Entrada expirada para '{movie_title}', actualizando...", file=sys.stderr)

    # FIX: inicializar url a None para evitar UnboundLocalError si falla get_imdb_url
    url = None
    raw_data = None
    ultimo_error = None

    for intento in range(1, MAX_RETRIES + 1):
        if intento > 1:
            print(f"      [REINTENTO {intento}/{MAX_RETRIES}] Esperando 3s antes de reintentar...", file=sys.stderr)
            time.sleep(3)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    locale="es-ES",
                    viewport={"width": 1280, "height": 800},
                )
                page = context.new_page()

                try:
                    print(f"[1/3] Buscando '{movie_title}' en IMDB...", file=sys.stderr)
                    url = get_imdb_url(page, movie_title)
                    print(f"[1/3] URL encontrada: {url}", file=sys.stderr)

                    print(f"[2/3] Extrayendo datos de la pelicula...", file=sys.stderr)
                    raw_data = extract_movie_data(page, url)

                finally:
                    context.close()
                    browser.close()

            # Si llegamos aqui, todo fue bien: salir del bucle de reintentos
            break

        except (RuntimeError, ValueError) as e:
            ultimo_error = e
            print(f"      [ERROR intento {intento}] {e}", file=sys.stderr)

    # Si todos los reintentos fallaron
    if url is None or raw_data is None:
        raise RuntimeError(
            f"No se pudo obtener informacion de '{movie_title}' "
            f"tras {MAX_RETRIES} intentos. Ultimo error: {ultimo_error}"
        )

    print(f"[3/3] Procesando y guardando en cache...", file=sys.stderr)
    result = {
        "titulo":   movie_title,
        "url_imdb": url,
        "nota":     raw_data["nota"],
        "votos":    raw_data["votos"],
        "sinopsis": str(raw_data["sinopsis"] or "").strip() or None,
        "director": str(raw_data["director"] or "").strip() or None,
        "duracion": raw_data["duracion"],
        "genero":   raw_data["genero"],
        # Timestamp para el TTL de la cache
        "_cached_at": datetime.now().isoformat(timespec="seconds"),
    }

    cache[cache_key] = result
    save_cache(cache)
    return result


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scraper de peliculas via IMDB + Playwright",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python movie_scraper_lucas.py "Inception"
  python movie_scraper_lucas.py "El Padrino" --campo nota
  python movie_scraper_lucas.py "2001" --json
  python movie_scraper_lucas.py "Matrix" --no-cache --json
  python movie_scraper_lucas.py "Interstellar" --campo genero
        """
    )
    parser.add_argument("pelicula", help="Nombre de la pelicula a consultar")
    parser.add_argument(
        "--campo",
        choices=["nota", "votos", "sinopsis", "director", "duracion", "genero"],
        help="Si se especifica, devuelve solo ese campo"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Salida en formato JSON"
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignorar cache y forzar nueva consulta"
    )
    args = parser.parse_args()

    try:
        data = get_movie_info(args.pelicula, use_cache=not args.no_cache)
    except (ValueError, RuntimeError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    # Filtrar campos internos de cache antes de mostrar
    public_data = {k: v for k, v in data.items() if not k.startswith("_")}

    if args.campo:
        value = public_data.get(args.campo)
        if args.json:
            print(json.dumps({args.campo: value}, ensure_ascii=False))
        else:
            # Si el campo es una lista (genero), mostrarlo legible
            if isinstance(value, list):
                print(f"{args.campo}: {', '.join(value)}")
            else:
                print(f"{args.campo}: {value}")
    else:
        if args.json:
            print(json.dumps(public_data, ensure_ascii=False, indent=2))
        else:
            votos_str    = f"{data['votos']:,}" if data["votos"] else "N/A"
            duracion_str = f"{data['duracion']} min" if data["duracion"] else "N/A"
            genero_str   = ", ".join(data["genero"]) if data["genero"] else "N/A"
            print(f"\n Pelicula : {data['titulo']}")
            print(f"   URL IMDB  : {data['url_imdb']}")
            print(f"   Nota      : {data['nota'] or 'N/A'}")
            print(f"   Votos     : {votos_str}")
            print(f"   Director  : {data['director'] or 'N/A'}")
            print(f"   Duracion  : {duracion_str}")
            print(f"   Genero    : {genero_str}")
            print(f"   Sinopsis  : {data['sinopsis'] or 'N/A'}")


if __name__ == "__main__":
    main()