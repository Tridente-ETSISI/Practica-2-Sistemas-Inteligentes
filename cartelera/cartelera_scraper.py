"""
cartelera_scraper.py

Obtiene la cartelera de cine de Madrid con Playwright (JS renderizado).
Dos fuentes independientes para redundancia:
  - Fuente 1: ecartelera.com  → get_cartelera_ecartelera()
  - Fuente 2: sensacine.com   → get_cartelera_sensacine()

Función principal: get_cartelera_madrid_playwright()
  Intenta ecartelera primero. Si falla o devuelve vacío, usa sensacine.

Cada película devuelta tiene este formato:
  {
    "titulo":        str,
    "url_ficha":     str,        # URL a la ficha en la fuente
    "genero":        str | None,
    "duracion_min":  int | None,
    "director":      str | None,
    "sinopsis":      str | None,
    "cines":         list[str],  # cines de Madrid donde se proyecta
    "num_cines":     int,        # número total de cines (ecartelera)
    "_fuente":       "ecartelera" | "sensacine"
  }

Uso standalone:
  python cartelera_scraper.py
  python cartelera_scraper.py --fuente sensacine
  python cartelera_scraper.py --cine "Kinépolis"
  python cartelera_scraper.py --json

Diagnóstico (si devuelve 0 películas):
  python cartelera_scraper.py --debug-html ecartelera
  → guarda /tmp/debug_ecartelera.html para inspeccionar los selectores CSS reales

NOTA SOBRE --cine:
  ecartelera.com no devuelve los nombres de cines individuales en su vista
  principal (solo el número total). El filtro --cine solo funciona con
  --fuente sensacine, que sí descarga la cartelera por cine.
"""

import argparse
import json
import re
import sys

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN PLAYWRIGHT
# ──────────────────────────────────────────────────────────────────────────────

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

# ──────────────────────────────────────────────────────────────────────────────
# HELPERS INTERNOS
# ──────────────────────────────────────────────────────────────────────────────

def _make_context(playwright):
    """Crea browser + context con evasión anti-bot."""
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
        },
    )
    context.add_init_script(EVASION_JS)
    return browser, context


def _accept_cookies(page) -> None:
    """
    Intenta cerrar banners de cookies comunes.

    ecartelera.com usa consentmanager.net cuyo botón "Aceptar todo" es un <a>
    con id="cmpwelcomebtnyes", no un <button>. Mientras el banner está activo,
    el <body> tiene overflow:hidden y el contenido real no es accesible.

    Orden de selectores: primero los específicos de consentmanager (ecartelera),
    luego los genéricos para otras webs.

    FIX: solo se silencian PlaywrightTimeout (elemento no encontrado).
    Otros errores inesperados se loguean para no perder trazabilidad.
    """
    for selector in [
        # consentmanager.net (ecartelera.com)
        "#cmpwelcomebtnyes",
        "a.cmpboxbtnyes",
        # didomi (sensacine y otras)
        "#didomi-notice-agree-button",
        # Textos genéricos — <button> y <a>
        'button:has-text("Aceptar todo")',
        'a:has-text("Aceptar todo")',
        'button:has-text("Aceptar")',
        'button:has-text("Accept all")',
        'button:has-text("Accept")',
        '[aria-label="Agree"]',
        ".cc-accept",
    ]:
        try:
            page.click(selector, timeout=2500)
            # Esperar a que el body recupere el scroll (banner desaparece)
            page.wait_for_function(
                "document.body.style.overflow !== 'hidden'",
                timeout=3000,
            )
            return
        except PlaywrightTimeout:
            # Elemento no encontrado en esta página: es el caso normal
            continue
        except Exception as e:
            # Error inesperado: loguear sin interrumpir el flujo
            print(
                f"[cookies] Error inesperado con selector '{selector}': {e}",
                file=sys.stderr,
            )
            continue


def _scroll_page(page, steps: int = 5) -> None:
    """Scroll progresivo para activar lazy-load."""
    for i in range(steps):
        page.evaluate(f"window.scrollTo(0, {(i + 1) * 600})")
        page.wait_for_timeout(400)


def _clean(text: str | None) -> str | None:
    """Normaliza espacios y devuelve None si el resultado está vacío."""
    if not text:
        return None
    return re.sub(r"\s+", " ", text).strip() or None


def _parse_duration(text: str | None) -> int | None:
    """Convierte '1h 48min' / '108 min' / '1h' a minutos enteros."""
    if not text:
        return None
    hours = re.search(r"(\d+)\s*h", text)
    mins  = re.search(r"(\d+)\s*min", text)
    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if mins:
        total += int(mins.group(1))
    if not hours and not mins:
        m = re.search(r"(\d+)", text)
        if m:
            total = int(m.group(1))
    return total if total > 0 else None


def _save_debug_html(page, nombre: str) -> None:
    """Guarda el HTML renderizado para diagnóstico de selectores."""
    ruta = f"/tmp/debug_{nombre}.html"
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(page.content())
    print(f"[debug] HTML guardado en {ruta}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════════════════════
# FUENTE 1: ecartelera.com
# URL: https://www.ecartelera.com/cines/0,30,1.html  (30 = provincia Madrid)
#
# Estructura real del HTML (verificada mayo 2025):
#   La página lista películas en cartelera, una por div.pitem:
#
#   <div class="pitem">
#     <p class="title">
#       <a href="https://www.ecartelera.com/peliculas/nombre/">Título</a>
#     </p>
#     <p class="showtimes">
#       <a href=".../cartelera/">Horarios: N cines</a>
#     </p>
#   </div>
#
#   NOTA: los nombres de cines individuales NO están en esta vista.
#   Solo aparece el número total de cines en Madrid donde se proyecta.
#   Para filtrar por cine usa --fuente sensacine.
# ══════════════════════════════════════════════════════════════════════════════

ECARTELERA_URL = "https://www.ecartelera.com/cines/0,30,1.html"
ECARTELERA_BASE = "https://www.ecartelera.com"


def _parse_ecartelera_page(page) -> list[dict]:
    """
    Extrae películas del HTML renderizado de ecartelera.com.

    Estructura real verificada (mayo 2025): la página lista películas
    agrupadas por pelicula con div.pitem, NO por cine:

      <div class="pitem">
        <p class="title">
          <a href="https://www.ecartelera.com/peliculas/nombre/">Título</a>
        </p>
        <p class="showtimes">
          <a href=".../cartelera/">Horarios: N cines</a>
        </p>
      </div>

    Los nombres de cines individuales no están en esta vista; se deja
    cines=[] para que el enriquecimiento posterior pueda completarlos.
    """
    peliculas: list[dict] = []
    seen: set[str] = set()

    items = page.query_selector_all("div.pitem")
    print(f"[ecartelera] {len(items)} pitem encontrados.", file=sys.stderr)

    for item in items:
        titulo_el = item.query_selector("p.title a, .title a")
        if not titulo_el:
            continue

        titulo = _clean(titulo_el.inner_text())
        if not titulo or len(titulo) < 2 or titulo in seen:
            continue
        seen.add(titulo)

        href = titulo_el.get_attribute("href") or ""
        url_ficha = href if href.startswith("http") else ECARTELERA_BASE + href

        # Extraer numero de cines del texto "Horarios: N cines"
        showtimes_el = item.query_selector("p.showtimes a, .showtimes a")
        num_cines_str = _clean(showtimes_el.inner_text() if showtimes_el else None) or ""
        num_match = re.search(r"(\d+)", num_cines_str)
        num_cines = int(num_match.group(1)) if num_match else 0

        peliculas.append({
            "titulo":       titulo,
            "url_ficha":    url_ficha,
            "genero":       None,
            "duracion_min": None,
            "director":     None,
            "sinopsis":     None,
            "cines":        [],
            "num_cines":    num_cines,
            "_fuente":      "ecartelera",
        })

    return peliculas


def get_cartelera_ecartelera(
    filtro_cine: str | None = None,
    debug_html: bool = False,
) -> list[dict]:
    """
    Descarga la cartelera de Madrid desde ecartelera.com usando Playwright.

    Lanza RuntimeError si el scraping falla o devuelve 0 películas,
    para que el orquestador pueda activar el fallback a sensacine.

    FIX: filtro_cine con ecartelera emite advertencia en lugar de devolver
    silenciosamente 0 películas, ya que esta fuente no incluye nombres de
    cines individuales. Para filtrar por cine, usar --fuente sensacine.
    """
    # FIX: advertir al usuario antes de empezar si va a usar --cine con esta fuente
    if filtro_cine:
        print(
            "[ecartelera] AVISO: ecartelera no devuelve nombres de cines individuales. "
            "El filtro por cine no tendrá efecto. Usa --fuente sensacine para filtrar por cine.",
            file=sys.stderr,
        )

    print("[ecartelera] Cargando cartelera de Madrid...", file=sys.stderr)

    with sync_playwright() as p:
        browser, context = _make_context(p)
        page = context.new_page()
        try:
            page.goto(ECARTELERA_URL, wait_until="domcontentloaded", timeout=30000)
            _accept_cookies(page)

            try:
                # div.pitem es el selector real en la versión actual de ecartelera
                page.wait_for_selector("div.pitem", timeout=10000)
            except PlaywrightTimeout:
                # Si no aparece en 10s, dar tiempo extra al JS
                page.wait_for_timeout(4000)

            _scroll_page(page, steps=6)
            page.wait_for_timeout(1000)

            if debug_html:
                _save_debug_html(page, "ecartelera")

            peliculas = _parse_ecartelera_page(page)

        finally:
            # FIX aplicado previamente: browser.close() solo una vez, siempre en finally
            page.close()
            browser.close()

    print(f"[ecartelera] {len(peliculas)} películas extraídas.", file=sys.stderr)

    if not peliculas:
        # Lanzar excepción para activar el fallback en get_cartelera_madrid_playwright
        raise RuntimeError(
            "ecartelera devolvió 0 películas. "
            "Ejecuta con --debug-html ecartelera para inspeccionar los selectores."
        )

    # FIX: no aplicar filtro_cine con ecartelera porque cines=[] siempre.
    # Se devuelven todas las películas. El usuario ya fue avisado arriba.
    return peliculas


# ══════════════════════════════════════════════════════════════════════════════
# FUENTE 2: sensacine.com
# URLs:
#   Películas en cartelera: https://www.sensacine.com/peliculas/en-cines/
#   Cines de Madrid:        https://www.sensacine.com/cines/madrid/
#
# Estructura esperada:
#   <article class="card entity-card ...">
#     <a class="meta-title-link" href="/peliculas/xxx/">Título</a>
#     <span class="what-time-bloc-txt">Género</span>
#     <div class="meta-body-direction"><span>Director</span></div>
#     <div class="synopsis-text">Sinopsis...</div>
#     <span class="runtime">1h 48min</span>
#   </article>
#
# NOTA: si los selectores devuelven 0 resultados, usar:
#   python cartelera_scraper.py --debug-html sensacine
# para inspeccionar el HTML real y actualizar los selectores.
# ══════════════════════════════════════════════════════════════════════════════

SENSACINE_CARTELERA_URL    = "https://www.sensacine.com/peliculas/en-cines/"
SENSACINE_CINES_MADRID_URL = "https://www.sensacine.com/cines/madrid/"


def _parse_sensacine_peliculas(page) -> list[dict]:
    """
    Extrae la lista de películas en cartelera desde sensacine.com/peliculas/en-cines/
    Los cines se rellenan en un paso posterior con _parse_sensacine_cines().
    """
    peliculas: list[dict] = []

    cards = page.query_selector_all(
        "article.card, article[class*='entity-card'], "
        ".card-movie, [class*='movie-card'], li.mdl"
    )
    print(f"[sensacine] {len(cards)} tarjetas encontradas.", file=sys.stderr)

    for card in cards:
        enlace = card.query_selector(
            "a.meta-title-link, a[class*='title'], h2 a, h3 a, .title a"
        )
        if not enlace:
            continue

        titulo = _clean(enlace.inner_text())
        if not titulo or len(titulo) < 2:
            continue

        href = enlace.get_attribute("href") or ""
        url_ficha = (
            "https://www.sensacine.com" + href if href.startswith("/") else href
        )

        genero_el = card.query_selector(
            ".what-time-bloc-txt, .genre, [class*='genre'], "
            ".meta-body-item:first-child span"
        )
        director_el = card.query_selector(
            ".meta-body-direction span, [class*='director'] span, "
            "[class*='direction'] a"
        )
        sinopsis_el = card.query_selector(
            ".synopsis-text, .synopsis, [class*='synopsis'], .description"
        )
        duracion_el = card.query_selector(
            ".runtime, time, [class*='runtime'], [class*='duration']"
        )

        peliculas.append({
            "titulo":       titulo,
            "url_ficha":    url_ficha,
            "genero":       _clean(genero_el.inner_text()   if genero_el   else None),
            "duracion_min": _parse_duration(duracion_el.inner_text() if duracion_el else None),
            "director":     _clean(director_el.inner_text() if director_el else None),
            "sinopsis":     _clean(sinopsis_el.inner_text() if sinopsis_el else None),
            "cines":        [],
            "num_cines":    0,
            "_fuente":      "sensacine",
        })

    return peliculas


def _parse_sensacine_cines(page) -> dict[str, list[str]]:
    """
    Extrae qué películas están en qué cines de Madrid desde sensacine.com/cines/madrid/
    Devuelve: { titulo_pelicula: [cine1, cine2, ...] }
    """
    resultado: dict[str, list[str]] = {}

    cine_blocks = page.query_selector_all(
        "div.theater-block, [class*='theater-block'], "
        "article.cinema, [class*='cinema-item']"
    )
    print(f"[sensacine] {len(cine_blocks)} cines encontrados.", file=sys.stderr)

    for block in cine_blocks:
        nombre_el = block.query_selector(
            "h2 a, h3 a, .theater-name a, [class*='cinema-name'] a, "
            "[class*='theater-name']"
        )
        nombre_cine = (
            _clean(nombre_el.inner_text() if nombre_el else None) or "Cine desconocido"
        )

        peli_links = block.query_selector_all(
            "a.meta-title-link, [class*='movie'] a, li a[href*='pelicula']"
        )
        for link in peli_links:
            titulo = _clean(link.inner_text())
            if not titulo:
                continue
            resultado.setdefault(titulo, [])
            if nombre_cine not in resultado[titulo]:
                resultado[titulo].append(nombre_cine)

    return resultado


def _match_cines(
    peliculas: list[dict],
    cines_por_pelicula: dict[str, list[str]],
) -> None:
    """
    Asigna in-place la lista de cines a cada película.
    Intenta coincidencia exacta primero; si falla, búsqueda tolerante por
    subcadena, pero solo si el título más corto tiene al menos 5 caracteres
    para evitar falsos positivos con títulos muy cortos (p.ej. "It").
    """
    for peli in peliculas:
        cines = cines_por_pelicula.get(peli["titulo"], [])
        if not cines:
            tl = peli["titulo"].lower()
            # Solo buscar si el título es suficientemente largo
            if len(tl) >= 5:
                for titulo_cines, lista_cines in cines_por_pelicula.items():
                    tc = titulo_cines.lower()
                    # Exigimos que ambos tengan longitud mínima para el match parcial
                    if len(tc) >= 5 and (tl in tc or tc in tl):
                        cines = lista_cines
                        break
        peli["cines"] = cines
        peli["num_cines"] = len(cines)


def get_cartelera_sensacine(
    filtro_cine: str | None = None,
    debug_html: bool = False,
) -> list[dict]:
    """
    Descarga la cartelera de Madrid desde sensacine.com usando Playwright.
    Combina la lista de películas con la información de cines de Madrid.

    FIX: las dos páginas se cierran explícitamente antes de cerrar el browser.
    FIX: _accept_cookies solo se llama en la primera página; el contexto
         comparte cookies, por lo que el banner ya no aparece en la segunda.
    """
    print("[sensacine] Cargando cartelera de Madrid...", file=sys.stderr)

    with sync_playwright() as p:
        browser, context = _make_context(p)
        page  = context.new_page()
        page2 = context.new_page()
        try:
            # ── Paso A: lista de películas ─────────────────────────────────
            page.goto(SENSACINE_CARTELERA_URL, wait_until="domcontentloaded", timeout=30000)
            # FIX: _accept_cookies solo en la primera página del contexto
            _accept_cookies(page)
            try:
                page.wait_for_selector(
                    "article.card, article[class*='entity-card'], .card-movie",
                    timeout=10000,
                )
            except PlaywrightTimeout:
                page.wait_for_timeout(4000)
            _scroll_page(page, steps=8)
            page.wait_for_timeout(1000)

            if debug_html:
                _save_debug_html(page, "sensacine_peliculas")

            peliculas = _parse_sensacine_peliculas(page)
            print(f"[sensacine] {len(peliculas)} películas extraídas.", file=sys.stderr)

            # ── Paso B: cines de Madrid ────────────────────────────────────
            # FIX: no llamar _accept_cookies(page2); el contexto ya aceptó cookies
            page2.goto(SENSACINE_CINES_MADRID_URL, wait_until="domcontentloaded", timeout=30000)
            try:
                page2.wait_for_selector(
                    "div.theater-block, [class*='theater-block'], article.cinema",
                    timeout=10000,
                )
            except PlaywrightTimeout:
                page2.wait_for_timeout(4000)
            _scroll_page(page2, steps=6)
            page2.wait_for_timeout(1000)

            if debug_html:
                _save_debug_html(page2, "sensacine_cines")

            cines_por_pelicula = _parse_sensacine_cines(page2)

        finally:
            # FIX: cerrar páginas explícitamente antes del browser
            page.close()
            page2.close()
            browser.close()

    _match_cines(peliculas, cines_por_pelicula)

    if filtro_cine:
        fl = filtro_cine.lower()
        peliculas = [p for p in peliculas if any(fl in c.lower() for c in p["cines"])]
        print(f"[sensacine] {len(peliculas)} películas en '{filtro_cine}'.", file=sys.stderr)

    return peliculas


# ══════════════════════════════════════════════════════════════════════════════
# FUNCIÓN PRINCIPAL — orquesta ambas fuentes
# ══════════════════════════════════════════════════════════════════════════════

def get_cartelera_madrid_playwright(
    filtro_cine: str | None = None,
    fuente: str = "auto",
    debug_html: bool = False,
) -> list[dict]:
    """
    Obtiene la cartelera de cine de Madrid.

    Parámetros:
      filtro_cine : solo devuelve películas en ese cine (coincidencia parcial).
                    Solo funciona con fuente="sensacine". Con ecartelera se emite
                    un aviso y se devuelven todas las películas.
      fuente      : "auto"       → ecartelera primero, sensacine si falla/vacío.
                    "ecartelera" → fuerza ecartelera.
                    "sensacine"  → fuerza sensacine.
      debug_html  : guarda HTML renderizado en /tmp/ para depurar selectores.

    Devuelve lista de dicts: titulo, url_ficha, genero, duracion_min,
                             director, sinopsis, cines, num_cines, _fuente.
    """
    if fuente == "ecartelera":
        return get_cartelera_ecartelera(filtro_cine, debug_html=debug_html)

    if fuente == "sensacine":
        return get_cartelera_sensacine(filtro_cine, debug_html=debug_html)

    # "auto": ecartelera primero, sensacine como fallback
    try:
        return get_cartelera_ecartelera(filtro_cine, debug_html=debug_html)
    except Exception as e:
        print(
            f"[cartelera] ecartelera falló: {e}\n  → Probando sensacine...",
            file=sys.stderr,
        )

    return get_cartelera_sensacine(filtro_cine, debug_html=debug_html)


# ──────────────────────────────────────────────────────────────────────────────
# CLI para pruebas standalone
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cartelera de Madrid con Playwright",
        epilog=(
            "NOTA: --cine solo funciona con --fuente sensacine. "
            "ecartelera no devuelve nombres de cines individuales."
        ),
    )
    parser.add_argument("--cine",   help="Filtrar por nombre de cine (parcial, solo con sensacine)")
    parser.add_argument("--fuente", choices=["auto", "ecartelera", "sensacine"], default="auto")
    parser.add_argument("--json",   action="store_true", help="Salida JSON")
    parser.add_argument(
        "--debug-html",
        choices=["ecartelera", "sensacine"],
        metavar="FUENTE",
        help="Guarda el HTML renderizado en /tmp/ para inspeccionar selectores CSS",
    )
    args = parser.parse_args()

    debug = args.debug_html is not None
    fuente = args.debug_html if debug else args.fuente

    peliculas = get_cartelera_madrid_playwright(
        filtro_cine=args.cine,
        fuente=fuente,
        debug_html=debug,
    )

    if args.json:
        print(json.dumps(peliculas, ensure_ascii=False, indent=2))
        return

    print(f"\n{'═' * 55}")
    print(f"  Cartelera de Madrid — {len(peliculas)} películas")
    print(f"{'═' * 55}")
    for p in peliculas:
        cines_str = ", ".join(p["cines"][:3]) if p["cines"] else f"{p.get('num_cines', 0)} cines"
        if len(p["cines"]) > 3:
            cines_str += f" (+{len(p['cines']) - 3})"
        dur = f"{p['duracion_min']} min" if p["duracion_min"] else "? min"
        print(f"\n🎬 {p['titulo']}  [{dur}]  [{p['_fuente']}]")
        if p.get("genero"):
            print(f"   Género   : {p['genero']}")
        if p.get("director"):
            print(f"   Director : {p['director']}")
        print(f"   Cines    : {cines_str}")
        if p.get("sinopsis"):
            sin = p["sinopsis"]
            print(f"   Sinopsis : {sin[:120]}{'...' if len(sin) > 120 else ''}")


if __name__ == "__main__":
    main()