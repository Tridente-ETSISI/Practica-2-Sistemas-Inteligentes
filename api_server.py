"""
api_server.py
Servidor REST que expone el agente de películas como API HTTP.
Usado por la interfaz web (index.html) y la Lambda de Alexa.

Endpoints:
  GET  /health
  GET  /pelicula?titulo=X[&campo=Y]
  POST /chat        { "mensaje": "..." }   → pipeline LLM completo
  GET  /cartelera   [?cine=X][&min_nota=Y][&fuente=Z]

Uso:
  python api_server.py              # 0.0.0.0:8080
  python api_server.py --port 9000
"""

import argparse
import json
import logging
import mimetypes
import os
import re
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Carpeta donde están index.html, styles.css y app.js.
# Por defecto: subcarpeta "web/" junto a este archivo.
# Se puede sobreescribir con la variable de entorno WEB_DIR.
WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web"))

sys.path.insert(0, os.path.dirname(__file__))

from scraper.movie_scraper import get_movie_info

try:
    from cartelera.cartelera_madrid import get_cartelera_madrid, enrich_with_imdb
    CARTELERA_AVAILABLE = True
except ImportError as e:
    print(f"[WARN] Cartelera no disponible: {e}", file=sys.stderr)
    CARTELERA_AVAILABLE = False

OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b")

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

CAMPOS_VALIDOS = {"nota", "votos", "sinopsis", "director", "duracion"}

# ──────────────────────────────────────────────
# LLM — interpretar intención del chat
# ──────────────────────────────────────────────

INTENT_PROMPT = """Analiza la siguiente pregunta sobre películas y devuelve SOLO un JSON válido:

{{
  "intencion": "pelicula" | "cartelera" | "desconocido",
  "titulo_pelicula": "nombre de la película o null",
  "campo": "nota" | "votos" | "sinopsis" | "director" | "duracion" | "todo" | null,
  "filtro_cine": "nombre del cine o null",
  "nota_minima": número o null
}}

Reglas:
- Pregunta por datos de película → intencion: "pelicula"
- Pregunta por cartelera/cines/qué ponen → intencion: "cartelera"
- "nota","puntuación","rating","valoración" → campo: "nota"
- "sinopsis","trama","de qué va" → campo: "sinopsis"
- "director","quien dirigió" → campo: "director"
- "duración","cuánto dura" → campo: "duracion"
- "votos","cuánta gente" → campo: "votos"
- Si quiere todo → campo: "todo"

Pregunta: {pregunta}

Responde SOLO el JSON."""


def ask_ollama_intent(pregunta: str) -> dict:
    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": INTENT_PROMPT.format(pregunta=pregunta),
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
        raw = json.loads(resp.read()).get("response", "{}")

    raw = re.sub(r"```json\s*|```\s*", "", raw)
    match = re.search(r'\{.*\}', raw, re.DOTALL)
    if match:
        return json.loads(match.group())
    return {"intencion": "desconocido"}


# ──────────────────────────────────────────────
# HTTP HANDLER
# ──────────────────────────────────────────────

class APIHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        logger.info(f"{self.address_string()} — {fmt % args}")

    # ── CORS (necesario para que el HTML pueda llamar a la API) ────────────
    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def send_json(self, data: dict | list, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    # ── Archivos estáticos ─────────────────────────────────────────────────
    def serve_static(self, file_path: Path) -> bool:
        """
        Intenta servir un archivo estático desde WEB_DIR.
        Devuelve True si lo sirvió, False si no existe.
        """
        if not file_path.exists() or not file_path.is_file():
            return False

        # Seguridad: evitar path traversal fuera de WEB_DIR
        try:
            file_path.resolve().relative_to(WEB_DIR.resolve())
        except ValueError:
            return False

        mime, _ = mimetypes.guess_type(str(file_path))
        mime = mime or "application/octet-stream"

        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", len(body))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)
        return True

    # ── GET ────────────────────────────────────────────────────────────────
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        url_path = parsed.path

        # ── Archivos estáticos: /, /index.html, /styles.css, /app.js ──────
        # Cualquier ruta que no empiece por /api/ y no sea un endpoint
        # conocido se trata como un archivo estático de WEB_DIR.
        API_ENDPOINTS = {"/health", "/pelicula", "/cartelera", "/chat"}
        if url_path not in API_ENDPOINTS:
            # / → index.html
            if url_path == "/":
                static_file = WEB_DIR / "index.html"
            else:
                # Quitar la barra inicial y resolver dentro de WEB_DIR
                static_file = WEB_DIR / url_path.lstrip("/")

            if self.serve_static(static_file):
                return

            # Si el archivo no existe y no es un endpoint de API → 404 HTML
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self._cors_headers()
            self.end_headers()
            self.wfile.write(b"404 Not Found")
            return

        # /health
        if url_path == "/health":
            self.send_json({"status": "ok", "ollama": OLLAMA_URL, "model": OLLAMA_MODEL})
            return

        # /pelicula?titulo=X[&campo=Y]
        if url_path == "/pelicula":
            titulo_list = params.get("titulo", [])
            if not titulo_list:
                self.send_json({"error": "Parámetro 'titulo' requerido"}, 400)
                return

            titulo = titulo_list[0]
            campo  = params.get("campo", [None])[0]

            if campo and campo not in CAMPOS_VALIDOS:
                self.send_json({"error": f"campo '{campo}' no válido. Usa: {', '.join(CAMPOS_VALIDOS)}"}, 400)
                return

            try:
                data = get_movie_info(titulo)
                self.send_json({campo: data.get(campo)} if campo else data)
            except Exception as e:
                logger.error(f"Error scraping '{titulo}': {e}")
                self.send_json({"error": str(e)}, 500)
            return

        # /cartelera[?cine=X&min_nota=Y&fuente=Z]
        if url_path == "/cartelera":
            if not CARTELERA_AVAILABLE:
                self.send_json({"error": "cartelera_scraper.py no encontrado"}, 503)
                return

            cine     = params.get("cine",     [None])[0]
            min_nota = params.get("min_nota",  [None])[0]
            # fuente   = params.get("fuente",    ["auto"])[0]

            try:
                peliculas = get_cartelera_madrid(filtro_cine=cine)
                peliculas = enrich_with_imdb(peliculas) 
                if min_nota:
                    try:
                        umbral = float(min_nota)
                        peliculas = [
                            p for p in peliculas
                            if p.get("nota_imdb") is not None and p["nota_imdb"] >= umbral
                        ]
                    except ValueError:
                        pass
                self.send_json(peliculas)
            except Exception as e:
                logger.error(f"Error cartelera: {e}")
                self.send_json({"error": str(e)}, 500)
            return

        self.send_json({"error": "Endpoint no encontrado"}, 404)

    # ── POST ───────────────────────────────────────────────────────────────
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        # /chat — pipeline completo con LLM
        if parsed.path == "/chat":
            try:
                body    = self.read_body()
                mensaje = body.get("mensaje", "").strip()

                if not mensaje:
                    self.send_json({"error": "Campo 'mensaje' requerido"}, 400)
                    return

                # 1. LLM interpreta la intención
                intent   = ask_ollama_intent(mensaje)
                intencion = intent.get("intencion", "desconocido")

                # 2. Ejecutar acción según intención
                if intencion == "pelicula":
                    titulo = intent.get("titulo_pelicula")
                    campo  = intent.get("campo", "todo")

                    if not titulo:
                        self.send_json({
                            "tipo": "texto",
                            "mensaje": "No identifiqué el nombre de la película. ¿Puedes escribirlo más claro?"
                        })
                        return

                    try:
                        datos = get_movie_info(titulo)
                        # Si solo piden un campo, filtrar
                        if campo and campo != "todo" and campo in CAMPOS_VALIDOS:
                            respuesta = {
                                "tipo":  "texto",
                                "mensaje": _formato_campo(titulo, campo, datos.get(campo)),
                                "datos": datos
                            }
                        else:
                            respuesta = {"tipo": "pelicula", "datos": datos}
                        self.send_json(respuesta)
                    except Exception as e:
                        self.send_json({"error": f"No pude obtener datos de '{titulo}': {e}"}, 500)

                elif intencion == "cartelera":
                    if not CARTELERA_AVAILABLE:
                        self.send_json({"error": "Módulo de cartelera no disponible"}, 503)
                        return

                    filtro_cine = intent.get("filtro_cine")
                    nota_minima = intent.get("nota_minima")
                    fuente      = "auto"

                    try:
                        peliculas = get_cartelera_madrid(filtro_cine=filtro_cine, fuente=fuente)
                        peliculas = enrich_with_imdb(peliculas) 
                        if nota_minima:
                            peliculas = [
                                p for p in peliculas
                                if p.get("nota_imdb") is not None and p["nota_imdb"] >= float(nota_minima)
                            ]
                        self.send_json({"tipo": "cartelera", "datos": peliculas})
                    except Exception as e:
                        self.send_json({"error": f"Error obteniendo cartelera: {e}"}, 500)

                else:
                    self.send_json({
                        "tipo": "texto",
                        "mensaje": (
                            "No entendí tu pregunta. Prueba con:\n"
                            "• «¿Cuál es la nota de Interstellar?»\n"
                            "• «¿Quién dirigió El Padrino?»\n"
                            "• «Cartelera de Madrid»"
                        )
                    })

            except Exception as e:
                logger.error(f"Error en /chat: {e}", exc_info=True)
                self.send_json({"error": str(e)}, 500)
            return

        self.send_json({"error": "Endpoint no encontrado"}, 404)


def _formato_campo(titulo: str, campo: str, valor) -> str:
    if valor is None:
        return f"No encontré '{campo}' para {titulo}."
        
    # Limpieza exhaustiva de los votos para evitar el crash del string
    votos_formateados = valor
    if campo == "votos":
        try:
            if isinstance(valor, str):
                # Quitamos puntos, comas, espacios y letras (por si viniera un "K" o "M")
                limpio = "".join(c for c in valor if c.isdigit())
                votos_formateados = f"{int(limpio):,}"
            else:
                votos_formateados = f"{int(valor):,}"
        except (ValueError, TypeError):
            # Si el scraping falló estrepitosamente, dejamos el string crudo como fallback
            votos_formateados = valor

    etiquetas = {
        "nota":     f"⭐ {titulo} tiene una nota de {valor}/10 en IMDB.",
        "votos":    f"🗳 {titulo} tiene {votos_formateados} votos en IMDB.",  # <-- Usamos la variable limpia
        "sinopsis": f"📖 Sinopsis de {titulo}: {valor}",
        "director": f"🎭 {titulo} fue dirigida por {valor}.",
        "duracion": f"⏱ {titulo} dura {valor} minutos.",
    }
    return etiquetas.get(campo, f"{campo}: {valor}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def run(host: str = "0.0.0.0", port: int = 8026):
    server = HTTPServer((host, port), APIHandler)
    logger.info(f"🚀 CineBot API en http://{host}:{port}")
    logger.info(f"   Endpoints: /health  /pelicula  /cartelera  /chat")
    logger.info(f"   Ollama:    {OLLAMA_URL} ({OLLAMA_MODEL})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Servidor detenido.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CineBot API Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8026)
    args = parser.parse_args()
    run(args.host, args.port)