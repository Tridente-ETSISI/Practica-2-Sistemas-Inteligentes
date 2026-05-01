# 🎬 Movie Agent — Agente Inteligente para Películas

Agente completo para consultar información de películas usando **LLM + Playwright + Telegram + Alexa**.

## Arquitectura del sistema

```
Usuario (Telegram / Alexa / CLI)
        │
        ▼
  [telegram_bot.py]  ←──────── lenguaje natural
        │
        ▼
  [Ollama LLM]  ←──── interpreta intención + genera código Playwright
        │
        ├── intención "película" ──▶ [movie_scraper.py]
        │                                   │
        │                                   ▼
        │                             IMDB (Playwright)
        │                                   │
        │                                   ▼
        │                            caché local (JSON)
        │
        └── intención "cartelera" ─▶ [cartelera_madrid.py]
                                            │
                                            ├── ecartelera.com
                                            └── movie_scraper.py (enriquece)
                                                        │
                                                        ▼
                                               Telegram / consola
```

## Estructura de archivos

```
movie_agent/
├── scraper/
│   ├── movie_scraper.py       # Scraper principal (CLI + librería)
│   └── cache.json             # Caché automática de películas
├── bot/
│   └── telegram_bot.py        # Bot de Telegram
├── cartelera/
│   └── cartelera_madrid.py    # Cartelera + perfil usuario + Telegram
├── alexa/
│   ├── alexa_lambda.py        # Lambda AWS para Alexa Skill
│   └── interaction_model.json # Modelo de interacción de Alexa
├── api_server.py              # API REST (bridge para Alexa)
├── setup.sh                   # Instalación automática
├── .env.example               # Variables de entorno de ejemplo
└── README.md
```

## Instalación rápida

```bash
git clone <repo>
cd movie_agent
chmod +x setup.sh
./setup.sh
```

O manualmente:
```bash
pip install python-telegram-bot playwright
playwright install chromium
ollama pull qwen2.5-coder:7b   # o el modelo que prefieras
cp .env.example .env
# Editar .env con tus credenciales
```

## Docker (recomendado para portabilidad)

Se incluye `Dockerfile` y `docker-compose.yml` para ejecutar el proyecto en contenedores.

### 1. Preparar variables de entorno

```bash
cp .env.example .env
```

Edita `.env` con tu token de Telegram y, si usas Ollama en tu máquina host, deja:

```bash
OLLAMA_URL=http://host.docker.internal:11434
```

### 2. Construir imagen

```bash
docker compose build
```

### 3. Levantar servicios principales

```bash
docker compose up -d api bot
```

Servicios incluidos:
- `api`: expone la API en `http://localhost:8080`
- `bot`: ejecuta el bot de Telegram en polling
- `cartelera` (opcional, perfil `tools`): tarea puntual para enviar cartelera por Telegram

### 4. Ver logs

```bash
docker compose logs -f api
docker compose logs -f bot
```

### 5. Parar servicios

```bash
docker compose down
```

### 6. Ejecutar cartelera puntual en contenedor

```bash
docker compose --profile tools run --rm cartelera
```

## Configuración

Edita `.env`:

```bash
TELEGRAM_BOT_TOKEN=tu_token_aqui        # De @BotFather
TELEGRAM_CHAT_ID=tu_chat_id             # Para envíos automáticos
OLLAMA_URL=http://localhost:11434        # O IP de otra máquina/VPN
OLLAMA_MODEL=qwen2.5-coder:7b
MOVIE_API_URL=http://100.x.x.x:8080    # IP Tailscale para Alexa
```

Si usas Docker y Ollama corre fuera del contenedor, usa `http://host.docker.internal:11434`.

## Uso

### 1. Bot de Telegram

```bash
source .env
python bot/telegram_bot.py
```

**Preguntas que entiende:**
- `¿Cuál es la nota de Interstellar?`
- `De qué va El Padrino`
- `¿Quién dirigió 2001: A Space Odyssey?`
- `Dame toda la info de Dune`
- `Cartelera de Madrid`
- `¿Qué ponen en Kinépolis?`
- `Películas con nota mayor a 7.5`

### 2. Scraper por línea de comandos

```bash
# Toda la información
python scraper/movie_scraper.py "Interstellar"

# Solo un campo
python scraper/movie_scraper.py "Interstellar" --campo nota
python scraper/movie_scraper.py "El Padrino" --campo director
python scraper/movie_scraper.py "Dune" --campo sinopsis

# Salida JSON
python scraper/movie_scraper.py "Inception" --json

# Forzar recarga (ignorar caché)
python scraper/movie_scraper.py "Interstellar" --no-cache
```

**Salida de ejemplo:**
```
🎬 Interstellar
   URL IMDB  : https://www.imdb.com/title/tt0816692/
   Nota      : 8.7
   Votos     : 2,180,000
   Director  : Christopher Nolan
   Duración  : 169 min
   Sinopsis  : A team of explorers travel through a wormhole in space...
```

### 3. Cartelera de Madrid

```bash
source .env

# Cartelera completa (filtrada por perfil de usuario)
python cartelera/cartelera_madrid.py

# Filtrar por cine
python cartelera/cartelera_madrid.py --cine "Kinépolis"

# Nota mínima personalizada
python cartelera/cartelera_madrid.py --min-nota 7.5

# Enviar por Telegram
python cartelera/cartelera_madrid.py --telegram

# Sin filtros (toda la cartelera)
python cartelera/cartelera_madrid.py --sin-filtro

# JSON para procesar
python cartelera/cartelera_madrid.py --json > cartelera.json
```

### 4. API REST (para Alexa)

```bash
source .env
python api_server.py --port 8080

# Pruebas:
curl "http://localhost:8080/pelicula?titulo=Interstellar"
curl "http://localhost:8080/pelicula?titulo=Interstellar&campo=nota"
curl "http://localhost:8080/health"
```

### 5. Alexa Skill

1. Crea un skill en [developer.amazon.com](https://developer.amazon.com)
2. En **Interaction Model → JSON Editor**: pega el contenido de `alexa/interaction_model.json`
3. Crea una Lambda en AWS con `alexa/alexa_lambda.py` como `lambda_function.py`
4. Variable de entorno Lambda: `MOVIE_API_URL=http://TU_IP_TAILSCALE:8080`
5. Conecta la Lambda al skill

**Frases de ejemplo:**
- *"Alexa, abre películas info"*
- *"¿Cuál es la nota de Interstellar?"*
- *"¿Quién dirigió 2001?"*
- *"De qué va Dune"*
- *"Cuánto dura El Padrino"*

## Perfil de usuario (cartelera)

Edita `DEFAULT_USER_PROFILE` en `cartelera/cartelera_madrid.py`:

```python
DEFAULT_USER_PROFILE = {
    "generos": {
        "Ciencia ficción": 6.0,   # Pasan películas de CF con nota >= 6
        "Drama": 7.0,             # Drama solo con nota >= 7
        "Comedia": 5.5,
        "Terror": 6.0,
    },
    "directores_favoritos": ["Christopher Nolan", "Denis Villeneuve"],
    "nota_minima_global": 5.0,    # Para géneros no listados
}
```

## Cron automático (lunes 9:00)

El `setup.sh` lo configura automáticamente. Para hacerlo manualmente:

```bash
crontab -e
# Añadir:
0 9 * * 1 cd /ruta/al/proyecto && source .env && python cartelera/cartelera_madrid.py --telegram >> /tmp/cartelera.log 2>&1
```

## Caché

El scraper guarda los resultados en `scraper/cache.json`.
Si una película ya fue consultada, se devuelven los datos guardados sin volver a scraper.

Para limpiar la caché:
```bash
rm scraper/cache.json
# o para una película concreta:
python scraper/movie_scraper.py "Interstellar" --no-cache
```

## Tecnologías

| Componente | Tecnología |
|-----------|-----------|
| Frontend usuario | Telegram Bot, Alexa Skill, CLI |
| Orquestador | Python 3.12 |
| LLM local | Ollama (qwen2.5-coder:7b) |
| Scraping | Playwright (Chromium) |
| Fuente películas | IMDB |
| Fuente cartelera | eCartelera.com |
| API bridge | HTTP server nativo Python |
| Caché | JSON local |
| Automatización | cron |
| VPN para Alexa | Tailscale |
