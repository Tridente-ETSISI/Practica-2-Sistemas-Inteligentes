"""
cartelera_madrid.py
Obtiene la cartelera de cine de Madrid desde ecartelera.com,
enriquece con datos de IMDB y filtra según el perfil del usuario.

Uso:
  python cartelera_madrid.py                        # cartelera completa
  python cartelera_madrid.py --cine "Cine Kinépolis" # filtrar por cine
  python cartelera_madrid.py --telegram              # enviar resultado por Telegram
  python cartelera_madrid.py --min-nota 7.0          # solo películas con nota >= 7.0

Cron (todos los lunes a las 9:00):
  0 9 * * 1 cd /ruta/al/proyecto && python cartelera/cartelera_madrid.py --telegram >> /var/log/cartelera.log 2>&1
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import asyncio

# ── Ajustar path para importar el scraper ─────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper"))
from movie_scraper import get_movie_info
from cartelera_scraper import get_cartelera_madrid_playwright

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Perfil de usuario por defecto (se puede editar o sobreescribir con --perfil JSON)
DEFAULT_USER_PROFILE = {
    "generos": {
        "Acción": 6.5,
        "Aventura": 6.5,
        "Animación": 7.0,
        "Comedia": 6.0,
        "Drama": 7.0,
        "Terror": 6.0,
        "Thriller": 6.5,
        "Ciencia ficción": 6.0,
        "Romance": 5.5,
        "Documental": 7.0,
    },
    "directores_favoritos": [],      # ej: ["Christopher Nolan", "Denis Villeneuve"]
    "nota_minima_global": 5.0,       # filtro base si el género no está en el perfil
}

BASE_URL = "https://www.ecartelera.com"
# CARTELERA_URL = f"{BASE_URL}/cartelera-cine-madrid/"
CARTELERA_URL = f"{BASE_URL}/cines/0,30,1.html"

# ──────────────────────────────────────────────
# DESCARGA Y PARSEO DE CARTELERA
# ──────────────────────────────────────────────

def download_page(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "es-ES,es;q=0.9",
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_cartelera(html: str) -> list[dict]:
    """
    Extrae lista de películas en cartelera desde ecartelera.com.
    Devuelve lista de dicts con: titulo, url, cines, genero
    """
    peliculas = []
    seen = set()

    # Patrón para bloques de película en ecartelera
    # Buscar enlaces a fichas de película
    pattern = re.compile(
        r'href="(/peliculas/[^"]+)"[^>]*>([^<]{3,80})</a>',
        re.IGNORECASE
    )

    for match in pattern.finditer(html):
        url_rel, titulo = match.group(1), match.group(2).strip()
        titulo = re.sub(r'\s+', ' ', titulo).strip()

        # Filtrar títulos que son claramente navegación / no películas
        if len(titulo) < 2 or titulo.lower() in {"ver más", "más info", "comprar", "trailer", "ver"}:
            continue
        if titulo in seen:
            continue
        seen.add(titulo)

        peliculas.append({
            "titulo": titulo,
            "url_ecartelera": BASE_URL + url_rel,
            "genero": None,
            "cines": []
        })

    return peliculas


def parse_cines_from_movie_page(html: str) -> tuple[list[str], str | None]:
    """Extrae lista de cines y género desde la página de una película en ecartelera."""
    cines = []
    genero = None

    # Extraer género
    gen_match = re.search(r'[Gg]énero[s]?\s*:?\s*<[^>]+>([^<]{3,40})</[^>]+>', html)
    if gen_match:
        genero = gen_match.group(1).strip()
    
    # Alternativa para género
    if not genero:
        gen_match2 = re.search(r'"genre"\s*:\s*"([^"]+)"', html)
        if gen_match2:
            genero = gen_match2.group(1).strip()

    # Extraer cines (nombres de sala)
    cine_pattern = re.compile(r'href="/cines/[^"]*"[^>]*>([^<]{3,60})</a>', re.IGNORECASE)
    for m in cine_pattern.finditer(html):
        nombre = m.group(1).strip()
        if nombre and nombre not in cines:
            cines.append(nombre)

    return cines, genero


def get_cartelera_madrid(filtro_cine: str | None = None) -> list[dict]:
    """Descarga y devuelve la cartelera de Madrid."""
    print("[Cartelera] Descargando cartelera de Madrid...", file=sys.stderr)
    # html = download_page(CARTELERA_URL)
    # peliculas = parse_cartelera(html)
    # if len(peliculas) == 0:
    #     print(f"[Cartelera] {len(peliculas)} películas encontradas.", file=sys.stderr)
    # else:
    peliculas = get_cartelera_madrid_playwright()  # Usar scraper con Playwright para obtener datos más completos
    print(f"[Cartelera] {len(peliculas)} películas encontradas con scraper.", file=sys.stderr)

    # Enriquecer con cines y género (solo las primeras 20 para no saturar)
    for peli in peliculas[:20]:
        try:
            peli_html = download_page(peli["url_ecartelera"])
            cines, genero = parse_cines_from_movie_page(peli_html)
            peli["cines"] = cines
            peli["genero"] = genero
        except Exception as e:
            print(f"[Cartelera] Error enriqueciendo {peli['titulo']}: {e}", file=sys.stderr)

    # Filtrar por cine si se especifica
    if filtro_cine:
        filtro_lower = filtro_cine.lower()
        peliculas = [
            p for p in peliculas
            if any(filtro_lower in c.lower() for c in p.get("cines", []))
        ]
        print(f"[Cartelera] {len(peliculas)} películas en '{filtro_cine}'.", file=sys.stderr)

    return peliculas


# ──────────────────────────────────────────────
# ENRIQUECER CON IMDB
# ──────────────────────────────────────────────

def enrich_with_imdb(peliculas: list[dict]) -> list[dict]:
    """Añade datos de IMDB a cada película."""
    enriched = []
    for peli in peliculas:
        try:
            print(f"[IMDB] Consultando '{peli['titulo']}'...", file=sys.stderr)
            imdb_data = get_movie_info(peli["titulo"])
            peli.update({
                "nota_imdb": imdb_data.get("nota"),
                "votos": imdb_data.get("votos"),
                "sinopsis": imdb_data.get("sinopsis"),
                "director": imdb_data.get("director"),
                "duracion": imdb_data.get("duracion"),
                "url_imdb": imdb_data.get("url_imdb"),
            })
        except Exception as e:
            print(f"[IMDB] Error con '{peli['titulo']}': {e}", file=sys.stderr)
            peli.update({"nota_imdb": None, "votos": None, "sinopsis": None,
                         "director": None, "duracion": None, "url_imdb": None})
        enriched.append(peli)
    return enriched


# ──────────────────────────────────────────────
# FILTRO POR PERFIL DE USUARIO
# ──────────────────────────────────────────────

def apply_user_profile(peliculas: list[dict], perfil: dict, nota_minima: float | None = None) -> list[dict]:
    """
    Filtra y ordena películas según el perfil del usuario.
    - Cada género tiene una nota mínima requerida
    - Los directores favoritos siempre pasan
    - Se puede añadir una nota mínima global
    """
    resultado = []
    generos = perfil.get("generos", {})
    directores_fav = [d.lower() for d in perfil.get("directores_favoritos", [])]
    nota_global = nota_minima or perfil.get("nota_minima_global", 5.0)

    for peli in peliculas:
        nota = peli.get("nota_imdb")
        genero = peli.get("genero", "")
        director = (peli.get("director") or "").lower()

        # Director favorito: siempre incluir
        if any(fav in director for fav in directores_fav if fav):
            peli["_razon_inclusion"] = f"Director favorito: {peli.get('director')}"
            resultado.append(peli)
            continue

        # Sin nota no podemos filtrar bien
        if nota is None:
            peli["_razon_inclusion"] = "Sin datos de nota"
            resultado.append(peli)
            continue

        # Determinar umbral para este género
        umbral = nota_global
        for gen_key, gen_nota in generos.items():
            if genero and gen_key.lower() in genero.lower():
                umbral = gen_nota
                break

        if nota >= umbral:
            peli["_razon_inclusion"] = f"Nota {nota} ≥ umbral {umbral} para género '{genero}'"
            resultado.append(peli)

    # Ordenar por nota descendente
    resultado.sort(key=lambda p: p.get("nota_imdb") or 0, reverse=True)
    return resultado


# ──────────────────────────────────────────────
# FORMATEAR MENSAJE
# ──────────────────────────────────────────────

def format_telegram_message(peliculas: list[dict], filtro_cine: str | None = None) -> str:
    header = "🎬 *Cartelera de Madrid*"
    if filtro_cine:
        header += f" — {filtro_cine}"
    header += "\n" + "─" * 30 + "\n"

    if not peliculas:
        return header + "No se encontraron películas con los filtros aplicados."

    lines = [header]
    for p in peliculas:
        nota_str = f"⭐ {p['nota_imdb']}" if p.get("nota_imdb") else "⭐ N/A"
        dur_str = f"⏱ {p['duracion']} min" if p.get("duracion") else ""
        dir_str = f"🎭 {p['director']}" if p.get("director") else ""
        cines_str = ""
        if p.get("cines"):
            cines_str = "🏛 " + ", ".join(p["cines"][:3])
            if len(p["cines"]) > 3:
                cines_str += f" (+{len(p['cines'])-3})"

        block = f"*{p['titulo']}*\n"
        block += f"{nota_str}  {dur_str}  {dir_str}\n".strip() + "\n"
        if p.get("sinopsis"):
            sinopsis = p["sinopsis"][:150] + "..." if len(p["sinopsis"]) > 150 else p["sinopsis"]
            block += f"_{sinopsis}_\n"
        if cines_str:
            block += f"{cines_str}\n"
        if p.get("url_imdb"):
            block += f"[Ver en IMDB]({p['url_imdb']})\n"
        block += "\n"
        lines.append(block)

    return "".join(lines)

# ──────────────────────────────────────────────
# ENVIAR POR TELEGRAM
# ──────────────────────────────────────────────

def send_telegram(text: str, token: str, chat_id: str):
    """Envía mensaje por Telegram dividiendo si es muy largo."""
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram tiene límite de 4096 chars por mensaje
    chunk_size = 4000
    chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]

    for chunk in chunks:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                raise RuntimeError(f"Error Telegram: {result}")


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cartelera de cine de Madrid con filtro por perfil")
    parser.add_argument("--cine", help="Filtrar por nombre de cine (parcial)")
    parser.add_argument("--min-nota", type=float, help="Nota mínima global (sobreescribe perfil)")
    parser.add_argument("--perfil", help="Ruta a JSON con perfil de usuario personalizado")
    parser.add_argument("--telegram", action="store_true", help="Enviar resultado por Telegram")
    parser.add_argument("--json", action="store_true", help="Salida en formato JSON")
    parser.add_argument("--sin-filtro", action="store_true", help="Mostrar toda la cartelera sin filtrar")
    args = parser.parse_args()

    # Cargar perfil
    perfil = DEFAULT_USER_PROFILE
    if args.perfil and os.path.exists(args.perfil):
        with open(args.perfil, "r", encoding="utf-8") as f:
            perfil = json.load(f)

    # Obtener cartelera
    peliculas = get_cartelera_madrid(filtro_cine=args.cine)

    # Enriquecer con IMDB
    peliculas = enrich_with_imdb(peliculas)

    # Aplicar filtro de perfil
    if not args.sin_filtro:
        peliculas = apply_user_profile(peliculas, perfil, nota_minima=args.min_nota)

    # Salida
    if args.json:
        print(json.dumps(peliculas, ensure_ascii=False, indent=2))
    elif args.telegram:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            print("ERROR: Define TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID como variables de entorno.", file=sys.stderr)
            sys.exit(1)
        msg = format_telegram_message(peliculas, filtro_cine=args.cine)
        send_telegram(msg, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        print(f"[OK] Mensaje enviado por Telegram ({len(peliculas)} películas).")
    else:
        msg = format_telegram_message(peliculas, filtro_cine=args.cine)
        # Quitar markdown para consola
        msg_plain = re.sub(r'[*_`\[\]()]', '', msg)
        print(msg_plain)


if __name__ == "__main__":
    main()
