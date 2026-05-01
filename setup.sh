#!/usr/bin/env bash
# setup.sh  –  Instala dependencias y configura el cron de cartelera

set -e
echo "🎬 Configurando Movie Agent..."

# ── 1. Dependencias Python ────────────────────────────────────────────────────
echo ""
echo "📦 Instalando dependencias Python..."
pip install python-telegram-bot playwright

# ── 2. Playwright: instalar Chromium ─────────────────────────────────────────
echo ""
echo "🌐 Instalando Chromium para Playwright..."
playwright install chromium
playwright install-deps chromium

# ── 3. Comprobar Ollama ───────────────────────────────────────────────────────
echo ""
echo "🤖 Comprobando Ollama..."
if ! command -v ollama &> /dev/null; then
    echo "⚠️  Ollama no encontrado. Instálalo desde https://ollama.com"
    echo "   Luego ejecuta: ollama pull qwen2.5-coder:7b"
else
    echo "✅ Ollama encontrado."
    echo "   Descargando modelo qwen2.5-coder:7b (puede tardar)..."
    ollama pull qwen2.5-coder:7b
fi

# ── 4. Archivo .env ───────────────────────────────────────────────────────────
echo ""
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "⚙️  Archivo .env creado. EDÍTALO con tus credenciales:"
    echo "   nano .env"
else
    echo "✅ .env ya existe."
fi

# ── 5. Cron para cartelera (lunes 9:00) ──────────────────────────────────────
echo ""
echo "⏰ Configurando cron para cartelera semanal..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CRON_CMD="0 9 * * 1 cd ${SCRIPT_DIR} && source .env && python cartelera/cartelera_madrid.py --telegram >> /tmp/cartelera.log 2>&1"

# Añadir al crontab si no existe ya
(crontab -l 2>/dev/null | grep -v "cartelera_madrid.py"; echo "${CRON_CMD}") | crontab -
echo "✅ Cron configurado: lunes a las 9:00"
echo "   Para verificar: crontab -l"

# ── 6. Resumen ────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "✅ Instalación completa"
echo ""
echo "Para arrancar el bot de Telegram:"
echo "  source .env && python bot/telegram_bot.py"
echo ""
echo "Para arrancar la API (Alexa):"
echo "  source .env && python api_server.py"
echo ""
echo "Para probar el scraper:"
echo "  python scraper/movie_scraper.py 'Interstellar'"
echo "  python scraper/movie_scraper.py 'Interstellar' --campo nota"
echo ""
echo "Para probar la cartelera:"
echo "  source .env && python cartelera/cartelera_madrid.py"
echo "  source .env && python cartelera/cartelera_madrid.py --cine 'Kinépolis'"
echo "  source .env && python cartelera/cartelera_madrid.py --telegram"
echo "════════════════════════════════════════════════════════════"
