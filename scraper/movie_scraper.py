"""
movie_scraper.py
Uso: python movie_scraper.py "Nombre de la película" [--campo nota|votos|sinopsis|director|duracion]

Flujo:
1. Busca la película en IMDB /find/ con Playwright + evasión completa (sin librerías externas)
2. Descarga el HTML de la página de detalle con el mismo contexto
3. Pasa el HTML al LLM local (Ollama) para que genere código Playwright de extracción
4. Ejecuta ese código y devuelve un diccionario con los campos pedidos
5. Cachea los resultados en cache.json para no repetir peticiones

Técnicas anti-detección usadas (sin playwright-stealth ni undetected-playwright):
- --headless=new: nuevo modo headless de Chrome, no expone las flags clásicas de automatización
- --disable-blink-features=AutomationControlled: elimina la flag JS de automatización
- context.add_init_script(): inyecta evasión JS ANTES de que cargue cualquier script de la página
- User-Agent, viewport, locale y timezone reales
- Scroll suave + delay para simular comportamiento humano
- Sin --enable-automation en los args
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# Fallback: OMDB API (datos de IMDB, gratuita).
# Se usa automáticamente si el scraping con Playwright falla.
# Requiere OMDB_API_KEY en .env — gratuita en https://www.omdbapi.com/apikey.aspx
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))

from omdb_fallback import get_movie_info_omdb, OMDBError
OMDB_AVAILABLE = True

CACHE_FILE = os.path.join(os.path.dirname(__file__), "cache.json")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")

# ──────────────────────────────────────────────────────────────────────────────
# Script JS de evasión inyectado vía add_init_script().
# Se ejecuta en CADA página ANTES de que cargue cualquier JS del sitio,
# por lo que IMDB no puede detectar las huellas de automatización.
# ──────────────────────────────────────────────────────────────────────────────
EVASION_JS = """
// 1. Eliminar navigator.webdriver (la señal más obvia de automatización)
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. Simular plugins reales de Chrome
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const arr = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ];
        arr.__proto__ = PluginArray.prototype;
        return arr;
    }
});

// 3. Idiomas reales
Object.defineProperty(navigator, 'languages', { get: () => ['es-ES', 'es', 'en-US', 'en'] });

// 4. Chrome runtime (ausente en headless puro)
if (!window.chrome) { window.chrome = {}; }
if (!window.chrome.runtime) { window.chrome.runtime = {}; }

// 5. Permissions API (en headless devuelve 'denied' por defecto, lo normalizamos)
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);

// 6. Ocultar que WebGL viene de un renderer de software (SwiftShader)
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';           // UNMASKED_VENDOR_WEBGL
    if (parameter === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
    return getParameter.call(this, parameter);
};

// 7. Dimensiones de pantalla coherentes con el viewport declarado
Object.defineProperty(screen, 'width',       { get: () => 1280 });
Object.defineProperty(screen, 'height',      { get: () => 800  });
Object.defineProperty(screen, 'availWidth',  { get: () => 1280 });
Object.defineProperty(screen, 'availHeight', { get: () => 772  });
Object.defineProperty(screen, 'colorDepth',  { get: () => 24   });
Object.defineProperty(screen, 'pixelDepth',  { get: () => 24   });
"""

# Args de Chromium para Docker y para no delatar automatización
CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",          # evita crashes de memoria en Docker
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--window-size=1280,800",
    "--disable-extensions",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ──────────────────────────────────────────────
# CACHÉ
# ──────────────────────────────────────────────

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ──────────────────────────────────────────────
# OLLAMA
# ──────────────────────────────────────────────

def ask_ollama(prompt: str) -> str:
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1}
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data.get("response", "")



def ask_ollama_lucas(prompt: str) -> str:
    """Llama a Ollama y devuelve el texto generado por el modelo."""
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1}
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            status_code = getattr(resp, "status", 200)
            raw_body = resp.read()

        if status_code != 200:
            raise RuntimeError(f"Ollama devolvió HTTP {status_code}")

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"La respuesta de Ollama no es un JSON válido: {e}\n"
                f"Respuesta recibida: {raw_body[:500]!r}"
            ) from e

        response_text = data.get("response")
        if not isinstance(response_text, str) or not response_text.strip():
            raise RuntimeError(
                f"Ollama no devolvió un campo 'response' válido. Respuesta completa: {data}"
            )

        return response_text.strip()

    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"Error HTTP al llamar a Ollama ({e.code} {e.reason}). "
            f"Detalle: {error_body[:500]}"
        ) from e

    except urllib.error.URLError as e:
        raise RuntimeError(
            f"No se pudo conectar con Ollama en {OLLAMA_URL}. "
            f"Comprueba que el servicio esté arrancado y accesible. Detalle: {e.reason}"
        ) from e

    except TimeoutError as e:
        raise RuntimeError(
            "Tiempo de espera agotado al llamar a Ollama."
        ) from e


# ──────────────────────────────────────────────
# CONTEXTO PLAYWRIGHT REUTILIZABLE
# ──────────────────────────────────────────────

def make_browser_context(playwright):
    """
    Crea un browser + context con todas las opciones anti-detección.
    Devuelve (browser, context) — recuerda cerrar ambos al terminar.
    Usa channel="chromium" con --headless=new para el nuevo modo headless
    que no expone las flags clásicas de automatización.
    """
    browser = playwright.chromium.launch(
        headless=True,
        args=CHROMIUM_ARGS,
        chromium_sandbox=False,
    )
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="es-ES",
        timezone_id="Europe/Madrid",
        # Simular que tiene hardware de verdad
        has_touch=False,
        is_mobile=False,
        java_script_enabled=True,
        # Cabeceras HTTP adicionales para parecer navegador real
        extra_http_headers={
            "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    # Inyectar evasión JS en CADA página del contexto, antes de cualquier script del sitio
    context.add_init_script(EVASION_JS)
    return browser, context


def accept_cookies(page):
    """Intenta aceptar el banner de cookies de IMDB si aparece."""
    for selector in [
        'button[data-testid="accept-button"]',
        'button:has-text("Accept")',
        'button:has-text("Aceptar")',
        '#__next button:has-text("Accept")',
    ]:
        try:
            page.click(selector, timeout=3000)
            page.wait_for_load_state("networkidle", timeout=5000)
            return
        except Exception:
            continue


def human_scroll(page):
    """Scroll suave para activar lazy-load y simular comportamiento humano."""
    page.evaluate("""
        () => new Promise(resolve => {
            let total = 0;
            const step = () => {
                window.scrollBy(0, 120);
                total += 120;
                if (total < 700) setTimeout(step, 80);
                else resolve();
            };
            step();
        })
    """)
    page.wait_for_timeout(600)


# ──────────────────────────────────────────────
# PASO 1 – Buscar película en IMDB /find/
# ──────────────────────────────────────────────

def get_imdb_url(movie_title: str) -> str:
    """
    Navega a imdb.com/find/ con Playwright + evasión completa y extrae
    la URL de la primera película encontrada.
    """
    print('PILLANDO URL')
    query = urllib.parse.quote_plus(movie_title)
    search_url = f"https://www.imdb.com/find/?q={query}&s=tt&ttype=ft&exact=false"
    print(f"[1/4] Buscando '{movie_title}' en IMDB...", file=sys.stderr)

    with sync_playwright() as p:
        print('PILLANDO URL 2')
        browser, context = make_browser_context(p)
        page = context.new_page()
        try:
            print('PILLANDO URL 3')
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            print('PILLANDO URL 4')
            accept_cookies(page)
            print('PILLANDO URL 5')
            human_scroll(page)

            # Selectores posibles para el primer resultado de búsqueda
            selectors = [
                'a.ipc-metadata-list-summary-item__t',
                '.ipc-metadata-list-summary-item__t',
                'li.find-title-result a',
                '.find-result-item a',
                'section[data-testid="find-results-section-title"] a',
            ]

            href = None
            for sel in selectors:
                try:
                    page.wait_for_selector(sel, state="attached", timeout=8000)
                    el = page.query_selector(sel)
                    if el:
                        href = el.get_attribute("href")
                        break
                except PlaywrightTimeoutError:
                    continue

            if not href:
                # Fallback: buscar en el HTML directamente
                html = page.content()
                match = re.search(r'href="(/title/tt\d+/)[^"]*"', html)
                if match:
                    href = match.group(1)
                else:
                    page.screenshot(path="/tmp/error_search.png")
                    raise ValueError(
                        f"No se encontró '{movie_title}' en IMDB. "
                        "Screenshot guardado en /tmp/error_search.png"
                    )

            match = re.search(r'(/title/tt\d+/)', href)
            if not match:
                raise ValueError(f"URL de resultado inesperada: {href}")

            url = "https://www.imdb.com" + match.group(1)
            print(f"[1/4] URL encontrada: {url}", file=sys.stderr)
            return url

        finally:
            browser.close()


def get_imdb_url_lucas(movie_title: str) -> str:
    """Busca la película en IMDB y devuelve la URL de su página."""
    movie_title = movie_title.strip()
    if not movie_title:
        raise ValueError("El título de la película no puede estar vacío")

    query = urllib.parse.quote_plus(movie_title)
    search_url = f"https://www.imdb.com/find/?q={query}&s=tt&ttype=ft"

    req = urllib.request.Request(
        search_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise RuntimeError(f"Error buscando '{movie_title}' en IMDB: {e}") from e

    matches = re.findall(r'href="(/title/tt\d+/)[^"]*"', html)
    if not matches:
        raise ValueError(f"No se encontró '{movie_title}' en IMDB")

    # Quitar duplicados manteniendo el orden
    unique_matches = []
    for match in matches:
        if match not in unique_matches:
            unique_matches.append(match)

    return "https://www.imdb.com" + unique_matches[0]


# ──────────────────────────────────────────────
# PASO 2 – Descargar HTML de la página de detalle
# ──────────────────────────────────────────────

def download_html(url: str) -> str:
    """Descarga el HTML de la página de detalle de la película con evasión completa."""
    print(f"[2/4] Descargando HTML de la página de la película...", file=sys.stderr)

    with sync_playwright() as p:
        browser, context = make_browser_context(p)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            accept_cookies(page)
            human_scroll(page)

            html = page.content()
            print(f"[2/4] HTML descargado ({len(html):,} chars).", file=sys.stderr)
            return html

        except Exception as e:
            try:
                page.screenshot(path="/tmp/error_detail.png")
                print("[2/4] Screenshot guardado en /tmp/error_detail.png", file=sys.stderr)
            except Exception:
                pass
            raise RuntimeError(f"Error descargando HTML de {url}: {e}")
        finally:
            browser.close()


def truncate_html(html: str, max_chars: int = 18000) -> str:
    """Extrae la sección principal del HTML para no saturar el contexto del LLM."""
    for tag in ['<main', '<div id="__next"', '<article', '<div class="ipc-page-content-container']:
        idx = html.find(tag)
        if idx != -1:
            return html[idx: idx + max_chars]
    return html[:max_chars]


# ──────────────────────────────────────────────
# PASO 3 – LLM genera código Playwright de extracción
# ──────────────────────────────────────────────

PLAYWRIGHT_PROMPT = """Eres un experto en web scraping con Python y Playwright.
Te voy a dar el HTML parcial de una página de IMDB de una película.
Tu tarea es generar un script Python COMPLETO y EJECUTABLE que use Playwright (sync_api) para:

1. Abrir la URL: {url}
2. Extraer estos datos de la película:
   - nota: puntuación numérica (ej: 8.3)
   - votos: número de votos (ej: 1234567)
   - sinopsis: descripción de la trama en español o inglés
   - director: nombre del director
   - duracion: duración en minutos (solo número entero, sin texto)
3. Imprimir ÚNICAMENTE un JSON válido con exactamente estas claves: nota, votos, sinopsis, director, duracion
   Ejemplo: {{"nota": 8.3, "votos": 1234567, "sinopsis": "...", "director": "...", "duracion": 148}}
4. Si no encuentras algún dato, ponlo como null

REGLAS IMPORTANTES:
- Usa SOLO playwright.sync_api (NO async)
- El script debe imprimir SOLO el JSON final, nada más
- NO uses print() para nada excepto el JSON final
- Usa estas opciones de lanzamiento para Docker y anti-detección:
    browser = p.chromium.launch(
        headless=True,
        args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
              "--disable-blink-features=AutomationControlled"]
    )
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    context.add_init_script("Object.defineProperty(navigator,'webdriver',{{get:()=>undefined}})")
- Usa wait_for_load_state("domcontentloaded") después de goto()
- Los selectores DEBEN basarse en el HTML que te muestro

HTML PARCIAL DE LA PÁGINA:
{html}

Genera SOLO el código Python, sin explicaciones, sin markdown, sin ```python.
"""

def generate_playwright_code(url: str, html: str) -> str:
    """Pide al LLM que genere el código Playwright para extraer los datos."""
    print(f"[3/4] Pidiendo al LLM ({OLLAMA_MODEL}) que genere código Playwright...", file=sys.stderr)
    prompt = PLAYWRIGHT_PROMPT.format(url=url, html=truncate_html(html))
    raw = ask_ollama(prompt)
    # Limpiar bloques markdown que el LLM pueda añadir igualmente
    raw = re.sub(r"```python\s*", "", raw)
    raw = re.sub(r"```\s*", "", raw)
    return raw.strip()


def generate_playwright_code_lucas(url: str, html: str) -> str:
    """Pide al LLM que genere el código Playwright para extraer los datos."""
    prompt = PLAYWRIGHT_PROMPT.format(url=url, html=truncate_html(html))
    raw = ask_ollama(prompt)

    # Limpiar posibles bloques markdown
    raw = re.sub(r"```python\s*", "", raw)
    raw = re.sub(r"```\s*", "", raw)
    code = raw.strip()

    if not code:
        raise RuntimeError("Ollama no devolvió código Playwright")

    if "sync_playwright" not in code:
        raise RuntimeError("El código generado no parece usar Playwright correctamente")

    return code


# ──────────────────────────────────────────────
# PASO 4 – Ejecutar el código generado
# ──────────────────────────────────────────────

def run_generated_code(code: str) -> dict:
    """Escribe el código generado en un archivo temporal y lo ejecuta en un subproceso."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, "PYTHONUNBUFFERED": "1"}
        )
        output = result.stdout.strip()

        if not output:
            raise RuntimeError(
                f"El código generado no produjo salida.\n"
                f"STDERR: {result.stderr[:600]}"
            )

        # Buscar el JSON en la salida por si hay texto extra
        json_match = re.search(r'\{[^{}]*\}', output, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return json.loads(output)

    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"JSON inválido en salida del código generado: {e}\n"
            f"Output: {result.stdout[:400]}\nStderr: {result.stderr[:400]}"
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Timeout: el código generado tardó más de 90 segundos")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def run_generated_code_lucas(code: str) -> dict:
    """Escribe el código en un archivo temporal, lo ejecuta y devuelve el JSON generado."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"El código generado terminó con error.\nStderr: {result.stderr.strip()}"
            )

        output = result.stdout.strip()
        if not output:
            raise RuntimeError("El código generado no devolvió ninguna salida")

        # Intentar parsear directamente toda la salida
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass

        # Si hay texto extra, intentar extraer un bloque JSON
        json_match = re.search(r'\{.*\}', output, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())

        raise RuntimeError(f"No se encontró un JSON válido en la salida: {output[:300]}")

    except subprocess.TimeoutExpired as e:
        raise RuntimeError("La ejecución del código generado superó el tiempo límite (60 s)") from e

    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"La salida del código generado no es un JSON válido.\n"
            f"Stdout: {result.stdout[:300]}\n"
            f"Stderr: {result.stderr[:300]}"
        ) from e

    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ──────────────────────────────────────────────
# FUNCIÓN PRINCIPAL
# ──────────────────────────────────────────────

def _scrape_playwright(movie_title: str) -> dict:
    """
    Intenta obtener los datos de la película mediante scraping con Playwright + LLM.
    Lanza una excepción si algo falla en cualquiera de los pasos.
    """
    url = get_imdb_url(movie_title)
    html = download_html(url)
    code = generate_playwright_code(url, html)

    print(f"[4/4] Ejecutando código generado...", file=sys.stderr)
    data = run_generated_code(code)

    def parse_votos(v):
        if v is None:
            return None
        cleaned = re.sub(r'[^\d]', '', str(v))
        return int(cleaned) if cleaned else None

    return {
        "titulo": movie_title,
        "url_imdb": url,
        "nota": float(data["nota"]) if data.get("nota") is not None else None,
        "votos": parse_votos(data.get("votos")),
        "sinopsis": str(data.get("sinopsis") or "").strip() or None,
        "director": str(data.get("director") or "").strip() or None,
        "duracion": int(data["duracion"]) if data.get("duracion") is not None else None,
        "_fuente": "playwright_scraping",
    }


def get_movie_info(movie_title: str, forzar_api: bool = False) -> dict:
    """
    Devuelve un diccionario con: titulo, url_imdb, nota, votos, sinopsis, director, duracion.

    Estrategia:
      1. Comprueba la caché local → devuelve sin red si existe
      2. Si forzar_api=True → va directo a OMDB sin intentar scraping
      3. Intenta scraping con Playwright + LLM
      4. Si el scraping falla → fallback automático a OMDB API
      5. Guarda el resultado en caché

    El campo '_fuente' del resultado indica qué método se usó:
      'playwright_scraping' | 'omdb_api' | 'cache'
    """
    cache = load_cache()
    cache_key = movie_title.lower().strip()

    if cache_key in cache:
        print(f"[CACHÉ] '{movie_title}' obtenido de caché local.", file=sys.stderr)
        return cache[cache_key]

    result = None
    scraping_error = None

    # ── Intento 1: Playwright + LLM ───────────────────────────────────────
    if not forzar_api:
        try:
            try:
                result = get_movie_data_fixed(movie_title)
            except:
                result = _scrape_playwright(movie_title)
                print(f"[OK] Datos obtenidos por scraping Playwright.", file=sys.stderr)
        except Exception as e:
            scraping_error = e
            print(
                f"[WARN] Scraping falló: {e}\n"
                f"       → Activando fallback OMDB API...",
                file=sys.stderr
            )

    # ── Intento 2: OMDB API ───────────────────────────────────────────────
    if result is None:
        if not OMDB_AVAILABLE:
            raise RuntimeError(
                f"Scraping falló y omdb_fallback.py no está disponible.\n"
                f"Error original: {scraping_error}"
            )
        try:
            result = get_movie_info_omdb(movie_title)
            print(f"[OK] Datos obtenidos por OMDB API.", file=sys.stderr)
        except Exception as omdb_error:
            raise RuntimeError(
                f"Tanto el scraping como la API fallaron.\n"
                f"  Scraping: {scraping_error}\n"
                f"  OMDB API: {omdb_error}"
            )

    cache[cache_key] = result
    save_cache(cache)
    return result


import re

# Función para poner la fecha en formato dd/mm/yyyy
def format_date(fecha_texto):
    meses = {
        "enero": "01",
        "febrero": "02",
        "marzo": "03",
        "abril": "04",
        "mayo": "05",
        "junio": "06",
        "julio": "07",
        "agosto": "08",
        "septiembre": "09",
        "octubre": "10",
        "noviembre": "11",
        "diciembre": "12"
    }

    try:
        # Quitar país entre paréntesis
        fecha_texto = fecha_texto.split("(")[0].strip()

        partes = fecha_texto.split(" de ")
        dia = partes[0].zfill(2)
        mes = meses[partes[1].lower()]
        año = partes[2]

        return f"{dia}/{mes}/{año}"
    except:
        return None
    

# Función para convertir el número de votos a un formato numérico
def format_num_votes(votos_texto):
    try:
        # Limpiar espacios raros
        votos_texto = votos_texto.replace('\xa0', '').strip()

        # 1. Miles
        if 'mil' in votos_texto:
            numero = float(votos_texto.replace('mil', '').replace(',', '.'))
            return int(numero * 1_000)

        # 2. Millones
        elif 'M' in votos_texto:
            numero = float(votos_texto.replace('M', '').replace(',', '.'))
            return int(numero * 1_000_000)

        # 3. Número normal
        else:
            return int(re.sub(r'\D', '', votos_texto))

    except:
        return None
    

# Función principal para obtener datos de la película
def get_movie_data_fixed(movie_title):
    data = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"])

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            locale="es-ES")

        page = context.new_page()

        # 1. Ir a IMDB España
        page.goto("https://www.imdb.com/es-es/")

        # 2. Buscar película
        page.wait_for_selector("input[name='q']")
        page.fill("input[name='q']", movie_title)

        # Esperar sugerencias del dropdown
        page.wait_for_selector("ul[role='listbox'] li", timeout=10000)

        # 3. Click en la primera sugerencia
        page.click("ul[role='listbox'] li a")

        # 4. Esperar página de la película
        page.wait_for_selector("h1")

         # Aceptar cookies si aparecen
        try:
            page.click("button:has-text('Accept')", timeout=3000)
        except:
            pass

        # SCROLL para cargar contenido dinámico
        for _ in range(10):
            page.mouse.wheel(0, 1000)
            page.wait_for_timeout(1000)

        # --- EXTRACCIÓN ---
        # Título
        try:
            data["titulo"] = page.query_selector("h1").inner_text().strip()
        except:
            data["titulo"] = None

        # Nota
        try:
            rating = page.query_selector("[data-testid='hero-rating-bar__aggregate-rating__score']").inner_text().split("\n")[0]
            data["nota"] = float(rating.replace(",", "."))
        except:
            data["nota"] = None

        # Número de votos
        try:
            numero_votos = page.locator("xpath=//*[@id='__next']/main/div/section[1]/section/div[3]/section/section/div[3]/div[2]/div[2]/div[1]/div/div[1]/a/span/div/div[2]/div[3]").inner_text()
            data["numero_votos"] = format_num_votes(numero_votos)
        except:
            data["numero_votos"] = None

        # Sinopsis
        try:
            data["sinopsis"] = page.query_selector("[data-testid='plot']").inner_text()
        except:
            data["sinopsis"] = None

        # Director
        try:
            data["director"] = page.locator("xpath=//*[@id='__next']/main/div/section[1]/section/div[3]/section/section/div[3]/div[2]/div[2]/div[2]/ul/li[1]/div/ul/li/a").inner_text()
        except:
            data["director"] = None

        # Duración
        try:
            data["duracion"] = page.locator("xpath=//*[@id='__next']/main/div/section[1]/section/div[3]/section/section/div[2]/div[1]/ul/li[3]").inner_text()
        except:
            data["duracion"] = None

        # Fecha de lanzamiento
        try:
            fecha_lanzamiento = page.locator("li:has(a:has-text('Fecha de lanzamiento')) a.ipc-metadata-list-item__list-content-item").first.inner_text()
            data["fecha_lanzamiento"] = format_date(fecha_lanzamiento)
        except:
            data["fecha_lanzamiento"] = None

        # Géneros
        try:
            data["generos"] = page.locator("li:has(span:has-text('Género')) a.ipc-metadata-list-item__list-content-item").all_inner_texts()
        except:
            data["generos"] = None

        browser.close()

    return data

def parse_nota(value):
    if value is None:
        return None

    text = str(value).strip().replace(",", ".")
    match = re.search(r"\d+(\.\d+)?", text)
    return float(match.group()) if match else None



def parse_duracion(value):
    if value is None:
        return None

    text = str(value).strip().lower()

    # Caso simple: "169" o "169 min"
    match = re.search(r"(\d+)", text)
    if "h" not in text and match:
        return int(match.group(1))

    # Caso tipo "2h 49m"
    match_h = re.search(r"(\d+)\s*h", text)
    match_m = re.search(r"(\d+)\s*m", text)

    horas = int(match_h.group(1)) if match_h else 0
    minutos = int(match_m.group(1)) if match_m else 0

    total = horas * 60 + minutos
    return total if total > 0 else None




def parse_votos(value):
    if value is None:
        return None

    text = str(value).strip().upper().replace(",", "").replace(".", "")

    # Caso simple: "2180000" o "2,180,000"
    if text.isdigit():
        return int(text)

    # Caso tipo "2.1M" o "2,1M"
    text_raw = str(value).strip().upper().replace(",", ".")
    match = re.search(r"(\d+(\.\d+)?)\s*M", text_raw)
    if match:
        return int(float(match.group(1)) * 1_000_000)

    match = re.search(r"(\d+(\.\d+)?)\s*K", text_raw)
    if match:
        return int(float(match.group(1)) * 1_000)

    return None




def get_movie_info_lucas(movie_title: str) -> dict:
    """
    Devuelve un diccionario con: nota, votos, sinopsis, director, duracion.
    Usa caché para evitar repetir peticiones.
    """
    movie_title = movie_title.strip()
    if not movie_title:
        raise ValueError("El título de la película no puede estar vacío")

    cache = load_cache()
    cache_key = movie_title.lower()

    if cache_key in cache:
        print(f"[CACHÉ] Datos de '{movie_title}' obtenidos de caché local.", file=sys.stderr)
        return cache[cache_key]

    print(f"[1/4] Buscando '{movie_title}' en IMDB...", file=sys.stderr)
    url = get_imdb_url(movie_title)
    print(f"[1/4] URL encontrada: {url}", file=sys.stderr)

    print(f"[2/4] Descargando HTML...", file=sys.stderr)
    html = download_html(url)

    print(f"[3/4] Pidiendo al LLM que genere código Playwright...", file=sys.stderr)
    code = generate_playwright_code(url, html)

    print(f"[4/4] Ejecutando código generado...", file=sys.stderr)
    data = run_generated_code(code)

    if not isinstance(data, dict):
        raise RuntimeError("El código generado no devolvió un diccionario válido")

    result = {
        "titulo": movie_title,
        "url_imdb": url,
        "nota": parse_nota(data.get("nota")),
        "votos": parse_votos(data.get("votos")),
        "sinopsis": str(data.get("sinopsis") or "").strip() or None,
        "director": str(data.get("director") or "").strip() or None,
        "duracion": parse_duracion(data.get("duracion")),
        }

    cache[cache_key] = result
    save_cache(cache)
    return result


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Scraper de películas via IMDB + LLM + Playwright (con fallback OMDB API)"
    )
    parser.add_argument("pelicula", help="Nombre de la película a consultar")
    parser.add_argument(
        "--campo",
        choices=["nota", "votos", "sinopsis", "director", "duracion"],
        help="Si se especifica, devuelve solo ese campo"
    )
    parser.add_argument("--json", action="store_true", help="Devolver resultado en formato JSON")
    parser.add_argument("--no-cache", action="store_true", help="Ignorar caché y forzar nueva consulta")
    parser.add_argument("--api", action="store_true", help="Usar directamente OMDB API sin intentar scraping")
    args = parser.parse_args()

    if args.no_cache:
        cache = load_cache()
        cache.pop(args.pelicula.lower().strip(), None)
        save_cache(cache)

    data = get_movie_info(args.pelicula, forzar_api=args.api)

    if args.campo:
        value = data.get(args.campo)
        if args.json:
            print(json.dumps({args.campo: value}, ensure_ascii=False))
        else:
            print(f"{args.campo}: {value}")
    else:
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            fuente = data.get("_fuente", "?")
            fuente_emoji = "🌐" if fuente == "playwright_scraping" else "📡" if fuente == "omdb_api" else "💾"
            print(f"\n🎬 {data['titulo']}  {fuente_emoji} [{fuente}]")
            print(f"   URL IMDB  : {data['url_imdb']}")
            print(f"   Nota      : {data['nota']}")
            print(f"   Votos     : {data['votos']:,}" if data['votos'] else "   Votos     : N/A")
            print(f"   Director  : {data['director']}")
            print(f"   Duración  : {data['duracion']} min" if data['duracion'] else "   Duración  : N/A")
            print(f"   Sinopsis  : {data['sinopsis']}")


if __name__ == "__main__":
    main()