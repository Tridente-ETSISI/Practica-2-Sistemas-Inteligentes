"""
api_server.py
Servidor REST mínimo que expone el scraper de películas como una API HTTP.
Lo ejecutas en tu máquina local (o VPS) y la Lambda de Alexa lo llama.

Uso:
  python api_server.py              # escucha en 0.0.0.0:8080
  python api_server.py --port 9000  # puerto personalizado

Endpoints:
  GET /pelicula?titulo=Interstellar           → JSON con todos los campos
  GET /pelicula?titulo=Interstellar&campo=nota → JSON solo con la nota
  GET /health                                  → {"status": "ok"}

Para exponer desde tu red local/VPN (Tailscale):
  - En Tailscale: la IP de tu máquina ya es accesible desde otros dispositivos.
  - En el Lambda de Alexa, pon MOVIE_API_URL=http://100.x.x.x:8080 (IP de Tailscale)
"""

import argparse
import json
import logging
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scraper"))
from scraper.movie_scraper import get_movie_info

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CAMPOS_VALIDOS = {"nota", "votos", "sinopsis", "director", "duracion"}


class MovieAPIHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        logger.info(f"{self.address_string()} - {format % args}")

    def send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        # ── /health ────────────────────────────────────────────────────────
        if parsed.path == "/health":
            self.send_json({"status": "ok"})
            return

        # ── /pelicula ──────────────────────────────────────────────────────
        if parsed.path == "/pelicula":
            titulo_list = params.get("titulo", [])
            if not titulo_list:
                self.send_json({"error": "Parámetro 'titulo' requerido"}, 400)
                return

            titulo = titulo_list[0]
            campo = params.get("campo", [None])[0]

            if campo and campo not in CAMPOS_VALIDOS:
                self.send_json({"error": f"Campo '{campo}' no válido. Usa: {', '.join(CAMPOS_VALIDOS)}"}, 400)
                return

            try:
                data = get_movie_info(titulo)
                if campo:
                    self.send_json({campo: data.get(campo)})
                else:
                    self.send_json(data)
            except Exception as e:
                logger.error(f"Error scraping '{titulo}': {e}", exc_info=True)
                self.send_json({"error": str(e)}, 500)
            return

        self.send_json({"error": "Endpoint no encontrado"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()


def run_server(host: str = "0.0.0.0", port: int = 8080):
    server = HTTPServer((host, port), MovieAPIHandler)
    logger.info(f"🚀 API server en http://{host}:{port}")
    logger.info(f"   Prueba: curl 'http://localhost:{port}/pelicula?titulo=Interstellar'")
    logger.info(f"   Prueba: curl 'http://localhost:{port}/pelicula?titulo=Interstellar&campo=nota'")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Servidor detenido.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="API REST para scraper de películas")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    run_server(args.host, args.port)
