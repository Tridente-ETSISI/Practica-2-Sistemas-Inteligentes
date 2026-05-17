"""
cartelera_madrid.py

Orquesta la cartelera de cine de Madrid:
  1. Obtiene películas en cartelera via cartelera_scraper.py (Playwright).
  2. Enriquece con datos de IMDB via movie_scraper.py.
  3. Filtra según el perfil del usuario (nota mínima por género, directores).
  4. Formatea y opcionalmente envía el resultado por Telegram.

Uso:
  python cartelera_madrid.py                               # cartelera completa
  python cartelera_madrid.py --cine "Cine Kinépolis"       # filtrar por cine (solo con sensacine)
  python cartelera_madrid.py --telegram                    # enviar por Telegram
  python cartelera_madrid.py --min-nota 7.0               # solo nota >= 7.0
  python cartelera_madrid.py --perfil perfil_ejemplo.json  # perfil personalizado
  python cartelera_madrid.py --sin-filtro                  # sin filtro de perfil
  python cartelera_madrid.py --json                        # salida JSON
  python cartelera_madrid.py --fuente sensacine            # forzar fuente

Cron (todos los lunes a las 9:00):
  0 9 * * 1 cd /ruta/proyecto && python cartelera/cartelera_madrid.py --telegram \
    --perfil cartelera/perfil_ejemplo.json >> /var/log/cartelera.log 2>&1

Variables de entorno:
  TELEGRAM_BOT_TOKEN  → token del bot de Telegram
  TELEGRAM_CHAT_ID    → chat_id destino del mensaje
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Path para importar módulos del proyecto ───────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scraper.movie_scraper import get_movie_info                         # noqa: E402
from cartelera.cartelera_scraper import get_cartelera_madrid_playwright  # noqa: E402

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURACIÓN
# ──────────────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID:   str = os.environ.get("TELEGRAM_CHAT_ID", "")

# Número máximo de películas a las que se consultará IMDB.
MAX_IMDB_ENRICHMENT: int = 30
# FIX: MAX_WORKERS definido una sola vez (había duplicado)
MAX_WORKERS: int = 8  # peticiones simultáneas a IMDB

# Perfil de usuario por defecto.
# Se puede sobreescribir con --perfil <ruta_json>.
DEFAULT_USER_PROFILE: dict = {
    "generos": {
        "Acción":          6.5,
        "Aventura":        6.5,
        "Animación":       7.0,
        "Comedia":         6.0,
        "Drama":           7.0,
        "Terror":          6.0,
        "Thriller":        6.5,
        "Ciencia ficción": 6.0,
        "Romance":         5.5,
        "Documental":      7.0,
    },
    "directores_favoritos": [],   # ej: ["Christopher Nolan", "Denis Villeneuve"]
    "nota_minima_global":   5.0,  # umbral si el género no está en el perfil
}


# ──────────────────────────────────────────────────────────────────────────────
# OBTENER CARTELERA
# ──────────────────────────────────────────────────────────────────────────────

def get_cartelera_madrid(
    filtro_cine: str | None = None,
    fuente: str = "auto",
) -> list[dict]:
    """
    Devuelve la cartelera de Madrid usando el scraper Playwright.

    FIX: ahora recibe y propaga el argumento `fuente` para que --fuente
    desde el CLI tenga efecto real (antes se ignoraba silenciosamente).

    El scraper ya incluye: titulo, url_ficha, genero, duracion_min,
    director, sinopsis, cines, num_cines, _fuente.
    Esta función NO hace peticiones adicionales; confía en lo que
    devuelve cartelera_scraper.py.
    """
    print("[Cartelera] Descargando cartelera de Madrid...", file=sys.stderr)
    peliculas = get_cartelera_madrid_playwright(
        filtro_cine=filtro_cine,
        fuente=fuente,
    )
    print(f"[Cartelera] {len(peliculas)} películas encontradas.", file=sys.stderr)
    return peliculas


# ──────────────────────────────────────────────────────────────────────────────
# ENRIQUECER CON IMDB
# ──────────────────────────────────────────────────────────────────────────────

def enrich_with_imdb(peliculas: list[dict]) -> list[dict]:
    """
    Añade nota, votos y url_imdb a cada película consultando IMDB.

    Las primeras MAX_IMDB_ENRICHMENT se consultan en paralelo con
    ThreadPoolExecutor para reducir el tiempo de espera notablemente.
    Las películas fuera de ese límite conservan nota_imdb=None.

    FIX: la lógica de captura de excepciones es consistente.
    Los errores se loguean dentro de _fetch y no se relanza desde
    future.result(), evitando la contradicción anterior.
    """
    # Inicializar campos IMDB en todas las películas
    for peli in peliculas:
        peli.setdefault("nota_imdb", None)
        peli.setdefault("votos",     None)
        peli.setdefault("url_imdb",  None)

    a_enriquecer = peliculas[:MAX_IMDB_ENRICHMENT]
    total = len(a_enriquecer)

    def _fetch(args: tuple[int, dict]) -> None:
        idx, peli = args
        # FIX: extraer titulo antes para evitar f-strings con índices anidados
        titulo = peli["titulo"]
        try:
            print(f"[IMDB] ({idx + 1}/{total}) '{titulo}'...", file=sys.stderr)
            imdb_data = get_movie_info(titulo)
            peli["nota_imdb"] = imdb_data.get("nota")
            peli["votos"]     = imdb_data.get("votos")
            peli["url_imdb"]  = imdb_data.get("url_imdb")
            # Solo sobreescribir si el scraper de cartelera no los obtuvo
            if not peli.get("director"):
                peli["director"] = imdb_data.get("director")
            if not peli.get("sinopsis"):
                peli["sinopsis"] = imdb_data.get("sinopsis")
        except Exception as e:
            # FIX: los errores se loguean aquí; future.result() no relanza
            print(f"[IMDB] Error con '{titulo}': {e}", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(_fetch, (i, peli))
            for i, peli in enumerate(a_enriquecer)
        ]
        # FIX: future.result() no intenta propagar excepciones ya capturadas
        for future in as_completed(futures):
            future.result()

    return peliculas


# ──────────────────────────────────────────────────────────────────────────────
# FILTRO POR PERFIL DE USUARIO
# ──────────────────────────────────────────────────────────────────────────────

def apply_user_profile(
    peliculas: list[dict],
    perfil: dict,
    nota_minima: float | None = None,
) -> list[dict]:
    """
    Filtra y ordena películas según el perfil del usuario.

    Reglas (en orden de prioridad):
      1. Si el director está en directores_favoritos → siempre incluir.
         NOTA: si director=None (IMDB no lo encontró), esta regla no aplica.
      2. Si nota_imdb es None y se pasó --min-nota → excluir.
         Si nota_imdb es None y solo hay perfil → incluir (beneficio de la duda).
      3. Si el género del perfil define una nota mínima → aplicarla.
      4. Si el género no está en el perfil → usar nota_minima o nota_minima_global.

    El resultado se ordena por nota_imdb descendente (None al final).
    """
    generos        = perfil.get("generos", {})
    directores_fav = [d.lower() for d in perfil.get("directores_favoritos", [])]
    nota_global    = nota_minima if nota_minima is not None else perfil.get("nota_minima_global", 5.0)

    resultado: list[dict] = []

    for peli in peliculas:
        nota     = peli.get("nota_imdb")
        genero   = peli.get("genero") or ""
        director = (peli.get("director") or "").lower()

        # Regla 1: director favorito siempre pasa
        # NOTA: si director es None/vacío esta regla no puede aplicarse,
        # aunque el director real pueda ser un favorito. Limitación conocida.
        if directores_fav and any(fav in director for fav in directores_fav if fav):
            peli["_razon_inclusion"] = f"Director favorito: {peli.get('director')}"
            resultado.append(peli)
            continue

        # Regla 2: sin nota
        #   - si se pasó --min-nota explícita → excluir (no podemos verificar el umbral)
        #   - si solo hay perfil por género    → incluir (beneficio de la duda)
        if nota is None:
            if nota_minima is not None:
                continue  # filtro explícito: sin nota no pasa
            peli["_razon_inclusion"] = "Sin datos de nota IMDB"
            resultado.append(peli)
            continue

        # Reglas 3-4: determinar umbral para este género
        umbral = nota_global
        for gen_key, gen_nota in generos.items():
            if gen_key.lower() in genero.lower():
                umbral = gen_nota
                break

        if nota >= umbral:
            peli["_razon_inclusion"] = (
                f"Nota {nota} >= umbral {umbral} (género: '{genero}')"
            )
            resultado.append(peli)

    resultado.sort(key=lambda p: p.get("nota_imdb") or 0.0, reverse=True)
    return resultado


# ──────────────────────────────────────────────────────────────────────────────
# FORMATEAR MENSAJE TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────

def _escape_markdown(text: str) -> str:
    """
    Escapa caracteres especiales de Markdown de Telegram (modo legacy/estándar).
    Aplica solo a: * _ ` [ ]
    NOTA: esta función es para parse_mode='Markdown' (legacy).
    Si se migra a MarkdownV2 hay que ampliarla con: . ! ( ) + - = { } | ~ >
    """
    return re.sub(r"([*_`\[\]])", r"\\\1", text)


def format_telegram_message(
    peliculas: list[dict],
    filtro_cine: str | None = None,
) -> str:
    """Construye el mensaje Telegram con la cartelera."""
    header = "🎬 *Cartelera de Madrid*"
    if filtro_cine:
        header += f" — {_escape_markdown(filtro_cine)}"
    header += "\n" + "─" * 30 + "\n"

    if not peliculas:
        return header + "No se encontraron películas con los filtros aplicados."

    lines = [header]
    for p in peliculas:
        titulo_esc = _escape_markdown(p["titulo"])

        nota_str = f"⭐ {p['nota_imdb']}" if p.get("nota_imdb") else "⭐ N/A"
        # duracion_min viene del scraper; duracion puede venir de IMDB
        duracion = p.get("duracion_min") or p.get("duracion")
        dur_str  = f"⏱ {duracion} min" if duracion else ""
        dir_str  = f"🎭 {_escape_markdown(p['director'])}" if p.get("director") else ""

        cines_str = ""
        if p.get("cines"):
            cines_str = "🏛 " + ", ".join(_escape_markdown(c) for c in p["cines"][:3])
            if len(p["cines"]) > 3:
                cines_str += f" (+{len(p['cines']) - 3})"
        elif p.get("num_cines"):
            cines_str = f"🏛 {p['num_cines']} cines en Madrid"

        fuente_badge = {
            "ecartelera": "🌐 _eCartelera_",
            "sensacine":  "🌐 _SensaCine_",
        }.get(p.get("_fuente", ""), "")

        block = f"*{titulo_esc}*\n"
        info_line = "  ".join(x for x in [nota_str, dur_str, dir_str] if x)
        if info_line:
            block += info_line + "\n"
        if p.get("sinopsis"):
            sin = p["sinopsis"]
            sin_esc = _escape_markdown(sin[:150] + ("..." if len(sin) > 150 else ""))
            block += f"_{sin_esc}_\n"
        if cines_str:
            block += cines_str + "\n"
        if p.get("url_imdb"):
            # Evitar formato inline de Markdown que puede romperse si la URL contiene
            # paréntesis u otros caracteres especiales. Enviar la URL en texto plano.
            block += f"Ver en IMDB: {p['url_imdb']}\n"
        if fuente_badge:
            block += fuente_badge + "\n"
        block += "\n"
        lines.append(block)

    return "".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# ENVIAR POR TELEGRAM
# ──────────────────────────────────────────────────────────────────────────────

def send_telegram(text: str, token: str, chat_id: str) -> None:
    """
    Envía un mensaje por Telegram dividiendo en chunks de 4000 chars.
    Lanza RuntimeError con el código HTTP si la API responde con error.

    Usa parse_mode='Markdown' (modo legacy). Los caracteres especiales
    deben haberse escapado previamente con _escape_markdown().
    """
    api_url    = f"https://api.telegram.org/bot{token}/sendMessage"
    chunk_size = 4000
    chunks     = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]

    for idx, chunk in enumerate(chunks, start=1):
        payload = json.dumps({
            "chat_id":                  chat_id,
            "text":                     chunk,
            "parse_mode":               "Markdown",
            "disable_web_page_preview": True,
        }).encode("utf-8")

        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    raise RuntimeError(f"Telegram API error (chunk {idx}): {result}")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"HTTP {e.code} al enviar chunk {idx} a Telegram: {body}"
            ) from e


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cartelera de cine de Madrid con filtro por perfil de usuario",
        epilog=(
            "NOTA: --cine solo tiene efecto con --fuente sensacine. "
            "ecartelera no devuelve nombres de cines individuales."
        ),
    )
    parser.add_argument("--cine",       help="Filtrar por nombre de cine (parcial, solo con sensacine)")
    parser.add_argument("--min-nota",   type=float, help="Nota mínima IMDB global (sobreescribe perfil)")
    parser.add_argument("--perfil",     help="Ruta a JSON con perfil de usuario personalizado")
    parser.add_argument("--telegram",   action="store_true", help="Enviar resultado por Telegram")
    parser.add_argument("--json",       action="store_true", help="Salida en formato JSON")
    parser.add_argument("--sin-filtro", action="store_true", help="Mostrar toda la cartelera sin filtrar")
    parser.add_argument(
        "--fuente",
        choices=["auto", "ecartelera", "sensacine"],
        default="auto",
        help="Fuente de datos del scraper (default: auto)",
    )
    args = parser.parse_args()

    # ── Cargar perfil ──────────────────────────────────────────────────────
    perfil = DEFAULT_USER_PROFILE
    if args.perfil:
        if not os.path.exists(args.perfil):
            print(f"ERROR: No se encontró el fichero de perfil: {args.perfil}", file=sys.stderr)
            sys.exit(1)
        with open(args.perfil, "r", encoding="utf-8") as f:
            perfil = json.load(f)

    # ── Validar variables Telegram antes de empezar ────────────────────────
    if args.telegram and (not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID):
        print(
            "ERROR: Define TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID como variables de entorno.",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── Pipeline principal ─────────────────────────────────────────────────
    # FIX: args.fuente se propaga correctamente a get_cartelera_madrid()
    peliculas = get_cartelera_madrid(filtro_cine=args.cine, fuente=args.fuente)
    peliculas = enrich_with_imdb(peliculas)

    if not args.sin_filtro:
        peliculas = apply_user_profile(peliculas, perfil, nota_minima=args.min_nota)

    # ── Salida ─────────────────────────────────────────────────────────────
    if args.json:
        print(json.dumps(peliculas, ensure_ascii=False, indent=2))

    elif args.telegram:
        msg = format_telegram_message(peliculas, filtro_cine=args.cine)
        send_telegram(msg, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        print(f"[OK] Mensaje enviado por Telegram ({len(peliculas)} películas).")

    else:
        msg = format_telegram_message(peliculas, filtro_cine=args.cine)
        # Quitar marcado Markdown para consola
        msg_plain = re.sub(r"[*_`\[\]()\\]", "", msg)
        print(msg_plain)


if __name__ == "__main__":
    main()