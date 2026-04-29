"""
cartelera_scraper.py

Obtiene la cartelera de cine de Madrid con Playwright (JS renderizado).
Dos fuentes independientes para redundancia:
  - Fuente 1: ecartelera.com  → get_cartelera_ecartelera()
  - Fuente 2: sensacine.com   → get_cartelera_sensacine()

Función principal: get_cartelera_madrid()
  Intenta ecartelera primero. Si falla, usa sensacine automáticamente.

Cada película devuelta tiene este formato:
  {
    "titulo":        str,
    "url_ficha":     str,   # URL a la ficha en la fuente
    "genero":        str | None,
    "duracion_min":  int | None,
    "director":      str | None,
    "sinopsis":      str | None,
    "cines":         list[str],  # cines de Madrid donde se proyecta
    "_fuente":       "ecartelera" | "sensacine"
  }

Uso standalone:
  python cartelera_scraper.py
  python cartelera_scraper.py --fuente sensacine
  python cartelera_scraper.py --cine "Kinépolis"
  python cartelera_scraper.py --json
"""

import argparse
import json
import re
import sys

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ──────────────────────────────────────────────
# CONFIGURACIÓN PLAYWRIGHT (igual que movie_scraper)
# ──────────────────────────────────────────────

EVASION_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es', 'en'] });
if (!window.chrome) window.chrome = { runtime: {} };
"""

CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--window-size=1280,900",
    "--disable-infobars",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _make_context(playwright):
    """Crea browser + context con evasión anti-bot. Reutilizable."""
    browser = playwright.chromium.launch(headless=True, args=CHROMIUM_ARGS)
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="es-ES",
        timezone_id="Europe/Madrid",
        extra_http_headers={
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-ch-ua": '"Chromium";v="124","Google Chrome";v="124"',
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }
    )
    context.add_init_script(EVASION_JS)
    return browser, context


def _accept_cookies(page):
    """Intenta cerrar banners de cookies comunes."""
    for selector in [
        '#didomi-notice-agree-button',
        'button:has-text("Aceptar todo")',
        'button:has-text("Aceptar")',
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        '[aria-label="Agree"]',
        '.cc-accept',
    ]:
        try:
            page.click(selector, timeout=2500)
            page.wait_for_timeout(800)
            return
        except Exception:
            continue


def _scroll_page(page, steps: int = 5):
    """Scroll progresivo para activar lazy-load."""
    for i in range(steps):
        page.evaluate(f"window.scrollTo(0, {(i + 1) * 600})")
        page.wait_for_timeout(400)


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r'\s+', ' ', text).strip() or None


def _parse_duration(text: str | None) -> int | None:
    """'1h 48min' / '108 min' / '1h' → 108"""
    if not text:
        return None
    hours = re.search(r'(\d+)\s*h', text)
    mins  = re.search(r'(\d+)\s*min', text)
    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if mins:
        total += int(mins.group(1))
    # Si solo hay un número suelto, asumir minutos
    if not hours and not mins:
        m = re.search(r'(\d+)', text)
        if m:
            total = int(m.group(1))
    return total if total > 0 else None


# ══════════════════════════════════════════════════════════════════════════════
# FUENTE 1: ecartelera.com
# URL cartelera Madrid: https://www.ecartelera.com/cines/0,30,1.html
#   (el 30 es el código de la provincia de Madrid)
# Estructura de la página:
#   - Lista de cines: .box-cine o article.cine-item
#   - Nombre del cine: h2.cine-name a  o  .nombre-cine
#   - Películas en ese cine: ul.pelis li  o  .pelicula-item
#   - Título película: .titulo-peli a  o  h3 a
#   - URL ficha: el href del enlace del título
# ══════════════════════════════════════════════════════════════════════════════

ECARTELERA_URL = "https://www.ecartelera.com/cines/0,30,1.html"


def _parse_ecartelera_page(page) -> list[dict]:
    """
    Extrae películas y cines del HTML renderizado de ecartelera.com.

    ecartelera agrupa la cartelera por CINE: primero aparece el cine,
    luego las películas que proyecta. Iteramos esa estructura.

    Si los selectores cambian, busca en el HTML:
      - El bloque de cada cine (suele ser un <article> o <div class="...cine...">)
      - El nombre del cine (suele ser un <h2> o <h3> dentro de ese bloque)
      - Las películas (suele ser una <ul> con <li> por cada película)
      - El enlace al título (un <a> dentro de cada <li>)
    """
    peliculas: dict[str, dict] = {}  # titulo → datos (para agrupar cines)

    # ── Selectores principales ─────────────────────────────────────────────
    # ecartelera.com usa una estructura tipo:
    #   <div class="cine-listado"> o <article class="cine">
    #     <h2 class="titulo-cine"><a>Nombre Cine</a></h2>
    #     <ul class="listado-pelis">
    #       <li class="peli-item">
    #         <a class="titulo" href="/peliculas/xxx">Título</a>
    #         <span class="genero">Acción</span>
    #         <span class="duracion">1h 48min</span>
    #       </li>
    #     </ul>
    #   </div>

    cine_blocks = page.query_selector_all(
        'div.cine-listado, article.cine, div.cine-item, '
        '[class*="cine-box"], [class*="box-cine"]'
    )

    if not cine_blocks:
        # Fallback: intentar con selectores más genéricos
        cine_blocks = page.query_selector_all('article, section.cine')

    print(f"[ecartelera] {len(cine_blocks)} bloques de cine encontrados.", file=sys.stderr)

    for block in cine_blocks:
        # Nombre del cine
        nombre_cine_el = block.query_selector(
            'h2 a, h3 a, .titulo-cine a, .nombre-cine, '
            '[class*="cine-name"] a, [class*="nombre"] a'
        )
        nombre_cine = _clean(nombre_cine_el.inner_text() if nombre_cine_el else None) or "Cine desconocido"

        # Películas en ese cine
        peli_items = block.query_selector_all(
            'li.peli-item, li.pelicula, [class*="peli-item"], '
            '[class*="pelicula-item"], ul li'
        )

        for item in peli_items:
            enlace = item.query_selector('a[href*="/peliculas/"], a.titulo, h3 a, h4 a')
            if not enlace:
                continue

            titulo = _clean(enlace.inner_text())
            if not titulo or len(titulo) < 2:
                continue

            href = enlace.get_attribute("href") or ""
            url_ficha = ("https://www.ecartelera.com" + href) if href.startswith("/") else href

            # Género y duración (opcionales)
            genero_el   = item.query_selector('.genero, [class*="genero"], .genre')
            duracion_el = item.query_selector('.duracion, [class*="duracion"], .runtime, time')
            genero   = _clean(genero_el.inner_text()   if genero_el   else None)
            duracion = _parse_duration(duracion_el.inner_text() if duracion_el else None)

            if titulo not in peliculas:
                peliculas[titulo] = {
                    "titulo":       titulo,
                    "url_ficha":    url_ficha,
                    "genero":       genero,
                    "duracion_min": duracion,
                    "director":     None,
                    "sinopsis":     None,
                    "cines":        [],
                    "_fuente":      "ecartelera",
                }

            if nombre_cine not in peliculas[titulo]["cines"]:
                peliculas[titulo]["cines"].append(nombre_cine)

    return list(peliculas.values())


def get_cartelera_ecartelera(filtro_cine: str | None = None) -> list[dict]:
    """
    Descarga la cartelera de Madrid desde ecartelera.com usando Playwright.
    Devuelve lista de películas con sus cines.
    """
    print("[ecartelera] Cargando cartelera de Madrid...", file=sys.stderr)

    with sync_playwright() as p:
        browser, context = _make_context(p)
        page = context.new_page()
        try:
            page.goto(ECARTELERA_URL, wait_until="domcontentloaded", timeout=30000)
            _accept_cookies(page)

            # Esperar a que cargue el contenido principal
            try:
                page.wait_for_selector(
                    'div.cine-listado, article.cine, [class*="cine-box"], li.peli-item',
                    timeout=10000
                )
            except PlaywrightTimeout:
                # Si no aparecen los selectores exactos, dar tiempo extra al JS
                page.wait_for_timeout(4000)

            _scroll_page(page, steps=6)
            page.wait_for_timeout(1000)

            peliculas = _parse_ecartelera_page(page)
            print(f"[ecartelera] {len(peliculas)} películas extraídas.", file=sys.stderr)

        except Exception as e:
            browser.close()
            raise RuntimeError(f"Error scraping ecartelera: {e}")
        finally:
            browser.close()

    # Filtro por cine
    if filtro_cine:
        fl = filtro_cine.lower()
        peliculas = [
            p for p in peliculas
            if any(fl in c.lower() for c in p["cines"])
        ]
        print(f"[ecartelera] {len(peliculas)} películas en '{filtro_cine}'.", file=sys.stderr)

    return peliculas


# ══════════════════════════════════════════════════════════════════════════════
# FUENTE 2: sensacine.com
# URL cartelera Madrid: https://www.sensacine.com/cines/madrid/
# Estructura de la página:
#   - Lista de películas: .jcarousel-item  o  article.card-movie
#   - O bien agrupada por cine igual que ecartelera
#   Sensacine tiene dos vistas: "por película" y "por cine".
#   Usamos la vista por película para simplificar:
#   https://www.sensacine.com/peliculas/en-cines/?page=1
#   y luego https://www.sensacine.com/cines/madrid/ para los cines.
# ══════════════════════════════════════════════════════════════════════════════

SENSACINE_CARTELERA_URL = "https://www.sensacine.com/peliculas/en-cines/"
SENSACINE_CINES_MADRID_URL = "https://www.sensacine.com/cines/madrid/"


def _parse_sensacine_peliculas(page) -> list[dict]:
    """
    Extrae la lista de películas en cartelera desde sensacine.com/peliculas/en-cines/

    Sensacine estructura cada película en una tarjeta:
      <article class="card entity-card entity-card-list ...">
        <a href="/peliculas/pelicula-xxxxx/" class="meta-title-link">Título</a>
        <span class="what-time-bloc-txt">Género</span>
        <div class="meta-body-item meta-body-direction">
          Director: <span>Nombre</span>
        </div>
        <div class="synopsis-text">Sinopsis...</div>
        <span class="runtime">1h 48min</span>
      </article>
    """
    peliculas = []

    # Selectores de tarjetas de película en sensacine
    cards = page.query_selector_all(
        'article.card, article[class*="entity-card"], '
        '.card-movie, [class*="movie-card"], li.mdl'
    )

    print(f"[sensacine] {len(cards)} tarjetas encontradas.", file=sys.stderr)

    for card in cards:
        # Título y URL
        enlace = card.query_selector(
            'a.meta-title-link, a[class*="title"], h2 a, h3 a, .title a'
        )
        if not enlace:
            continue

        titulo = _clean(enlace.inner_text())
        if not titulo or len(titulo) < 2:
            continue

        href = enlace.get_attribute("href") or ""
        url_ficha = ("https://www.sensacine.com" + href) if href.startswith("/") else href

        # Género
        genero_el = card.query_selector(
            '.what-time-bloc-txt, .genre, [class*="genre"], '
            '.meta-body-item:first-child span'
        )
        genero = _clean(genero_el.inner_text() if genero_el else None)

        # Director
        director_el = card.query_selector(
            '.meta-body-direction span, [class*="director"] span, '
            '[class*="direction"] a'
        )
        director = _clean(director_el.inner_text() if director_el else None)

        # Sinopsis
        sinopsis_el = card.query_selector(
            '.synopsis-text, .synopsis, [class*="synopsis"], .description'
        )
        sinopsis = _clean(sinopsis_el.inner_text() if sinopsis_el else None)

        # Duración
        duracion_el = card.query_selector('.runtime, time, [class*="runtime"], [class*="duration"]')
        duracion = _parse_duration(duracion_el.inner_text() if duracion_el else None)

        peliculas.append({
            "titulo":       titulo,
            "url_ficha":    url_ficha,
            "genero":       genero,
            "duracion_min": duracion,
            "director":     director,
            "sinopsis":     sinopsis,
            "cines":        [],  # se rellena después con _parse_sensacine_cines
            "_fuente":      "sensacine",
        })

    return peliculas


def _parse_sensacine_cines(page) -> dict[str, list[str]]:
    """
    Extrae qué películas están en qué cines de Madrid desde sensacine.com/cines/madrid/
    Devuelve un dict: titulo_pelicula → [cine1, cine2, ...]

    Sensacine agrupa por cine:
      <div class="theater-block">
        <h2 class="theater-name"><a>Nombre del Cine</a></h2>
        <ul class="theater-movies">
          <li><a class="meta-title-link">Título Película</a></li>
        </ul>
      </div>
    """
    resultado: dict[str, list[str]] = {}

    cine_blocks = page.query_selector_all(
        'div.theater-block, [class*="theater-block"], '
        'article.cinema, [class*="cinema-item"]'
    )

    print(f"[sensacine] {len(cine_blocks)} cines encontrados.", file=sys.stderr)

    for block in cine_blocks:
        nombre_el = block.query_selector(
            'h2 a, h3 a, .theater-name a, [class*="cinema-name"] a, '
            '[class*="theater-name"]'
        )
        nombre_cine = _clean(nombre_el.inner_text() if nombre_el else None) or "Cine desconocido"

        peli_links = block.query_selector_all(
            'a.meta-title-link, [class*="movie"] a, li a[href*="pelicula"]'
        )
        for link in peli_links:
            titulo = _clean(link.inner_text())
            if not titulo:
                continue
            if titulo not in resultado:
                resultado[titulo] = []
            if nombre_cine not in resultado[titulo]:
                resultado[titulo].append(nombre_cine)

    return resultado


def get_cartelera_sensacine(filtro_cine: str | None = None) -> list[dict]:
    """
    Descarga la cartelera de Madrid desde sensacine.com usando Playwright.
    Combina la lista de películas con la información de cines de Madrid.
    """
    print("[sensacine] Cargando cartelera de Madrid...", file=sys.stderr)

    with sync_playwright() as p:
        browser, context = _make_context(p)

        try:
            # ── Paso A: lista de películas en cartelera ────────────────────
            page = context.new_page()
            page.goto(SENSACINE_CARTELERA_URL, wait_until="domcontentloaded", timeout=30000)
            _accept_cookies(page)

            try:
                page.wait_for_selector(
                    'article.card, article[class*="entity-card"], .card-movie',
                    timeout=10000
                )
            except PlaywrightTimeout:
                page.wait_for_timeout(4000)

            _scroll_page(page, steps=8)
            page.wait_for_timeout(1000)

            peliculas = _parse_sensacine_peliculas(page)
            print(f"[sensacine] {len(peliculas)} películas extraídas.", file=sys.stderr)

            # ── Paso B: cines de Madrid ────────────────────────────────────
            page2 = context.new_page()
            page2.goto(SENSACINE_CINES_MADRID_URL, wait_until="domcontentloaded", timeout=30000)
            _accept_cookies(page2)

            try:
                page2.wait_for_selector(
                    'div.theater-block, [class*="theater-block"], article.cinema',
                    timeout=10000
                )
            except PlaywrightTimeout:
                page2.wait_for_timeout(4000)

            _scroll_page(page2, steps=6)
            page2.wait_for_timeout(1000)

            cines_por_pelicula = _parse_sensacine_cines(page2)

            # ── Combinar: asignar cines a cada película ────────────────────
            for peli in peliculas:
                # Buscar coincidencia de título (exacta o parcial)
                cines = cines_por_pelicula.get(peli["titulo"], [])
                if not cines:
                    # Búsqueda tolerante: el título de la lista puede tener pequeñas diferencias
                    titulo_lower = peli["titulo"].lower()
                    for titulo_cines, lista_cines in cines_por_pelicula.items():
                        if titulo_lower in titulo_cines.lower() or titulo_cines.lower() in titulo_lower:
                            cines = lista_cines
                            break
                peli["cines"] = cines

        except Exception as e:
            browser.close()
            raise RuntimeError(f"Error scraping sensacine: {e}")
        finally:
            browser.close()

    # Filtro por cine
    if filtro_cine:
        fl = filtro_cine.lower()
        peliculas = [
            p for p in peliculas
            if any(fl in c.lower() for c in p["cines"])
        ]
        print(f"[sensacine] {len(peliculas)} películas en '{filtro_cine}'.", file=sys.stderr)

    return peliculas


# ══════════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL — orquesta ambas fuentes
# ══════════════════════════════════════════════════════════════════════════════

def get_cartelera_madrid_playwright(
    filtro_cine: str | None = None,
    fuente: str = "auto"
) -> list[dict]:
    """
    Obtiene la cartelera de cine de Madrid.

    Parámetros:
      filtro_cine: si se especifica, solo devuelve películas en ese cine (coincidencia parcial)
      fuente:      "auto"       → intenta ecartelera, si falla usa sensacine
                   "ecartelera" → fuerza ecartelera
                   "sensacine"  → fuerza sensacine

    Devuelve lista de dicts con:
      titulo, url_ficha, genero, duracion_min, director, sinopsis, cines, _fuente
    """
    if fuente == "ecartelera":
        return get_cartelera_ecartelera(filtro_cine)

    if fuente == "sensacine":
        return get_cartelera_sensacine(filtro_cine)

    # "auto": ecartelera primero, sensacine como fallback
    try:
        resultado = get_cartelera_ecartelera(filtro_cine)
        if resultado:
            return resultado
        print("[cartelera] ecartelera devolvió 0 resultados, probando sensacine...", file=sys.stderr)
    except Exception as e:
        print(f"[cartelera] ecartelera falló: {e}\n  → Probando sensacine...", file=sys.stderr)

    return get_cartelera_sensacine(filtro_cine)


# ──────────────────────────────────────────────
# CLI para pruebas standalone
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cartelera de Madrid con Playwright")
    parser.add_argument("--cine",   help="Filtrar por nombre de cine (parcial)")
    parser.add_argument("--fuente", choices=["auto", "ecartelera", "sensacine"], default="auto")
    parser.add_argument("--json",   action="store_true", help="Salida JSON")
    args = parser.parse_args()

    peliculas = get_cartelera_madrid_playwright(filtro_cine=args.cine, fuente=args.fuente)

    if args.json:
        print(json.dumps(peliculas, ensure_ascii=False, indent=2))
        return

    print(f"\n{'═'*55}")
    print(f"  Cartelera de Madrid — {len(peliculas)} películas")
    print(f"{'═'*55}")
    for p in peliculas:
        cines_str = ", ".join(p["cines"][:3]) if p["cines"] else "cines no disponibles"
        if len(p["cines"]) > 3:
            cines_str += f" (+{len(p['cines'])-3})"
        dur = f"{p['duracion_min']} min" if p["duracion_min"] else "? min"
        print(f"\n🎬 {p['titulo']}  [{dur}]  [{p['_fuente']}]")
        if p.get("genero"):
            print(f"   Género   : {p['genero']}")
        if p.get("director"):
            print(f"   Director : {p['director']}")
        print(f"   Cines    : {cines_str}")
        if p.get("sinopsis"):
            print(f"   Sinopsis : {p['sinopsis'][:120]}...")


if __name__ == "__main__":
    main()
