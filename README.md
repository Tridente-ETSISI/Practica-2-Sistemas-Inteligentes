# 🎬 Movie Agent — Agente Inteligente para Películas

Agente completo para consultar información de películas usando **LLM + Playwright + Telegram + Alexa**.

## Arquitectura del sistema

```
Usuario (Telegram / Alexa / CLI / n8n)
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
                                            ├── ecartelera.com (fuente principal)
                                            ├── sensacine.com  (fallback automático)
                                            └── movie_scraper.py (enriquece con IMDB)
                                                        │
                                                        ▼
                                               Telegram / Email (n8n) / consola
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
│   ├── cartelera_scraper.py   # Scraper Playwright (ecartelera + sensacine)
│   ├── cartelera_madrid.py    # Orquestador: cartelera + IMDB + perfil + Telegram
│   └── perfil_ejemplo.json    # Perfil de usuario de ejemplo
├── alexa/
│   ├── alexa_lambda.py        # Lambda AWS para Alexa Skill
│   └── interaction_model.json # Modelo de interacción de Alexa
├── n8n/
│   └── workflows/
│       └── cartelera-email.json  # Workflow n8n: cartelera semanal por email
├── api_server.py              # API REST (bridge para Alexa y n8n)
├── setup.sh                   # Instalación automática
├── docker-compose.yml         # Servicios Docker (api, bot, cartelera, n8n, postgres)
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

## Docker (recomendado)

Se incluye `Dockerfile` y `docker-compose.yml` para ejecutar el proyecto en contenedores.

### 1. Preparar variables de entorno

```bash
cp .env.example .env
```

Edita `.env` con tu token de Telegram y, si usas Ollama en tu máquina host:

```bash
OLLAMA_URL=http://host.docker.internal:11434
TELEGRAM_BOT_TOKEN=tu_token_aqui
TELEGRAM_CHAT_ID=tu_chat_id
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
- `api`: expone la API REST en `http://localhost:8026`
- `bot`: ejecuta el bot de Telegram en polling
- `n8n`: workflow de automatización en `http://localhost:5678`
- `postgres`: base de datos de n8n
- `cartelera` (perfil `tools`): tarea puntual para enviar cartelera por Telegram

### 4. Ver logs

```bash
docker compose logs -f api
docker compose logs -f bot
docker compose logs -f n8n
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
OLLAMA_URL=http://localhost:11434        # O IP de otra máquina
OLLAMA_MODEL=qwen2.5-coder:7b
MOVIE_API_URL=http://100.x.x.x:8026    # IP Tailscale para Alexa
```

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

# Filtrar por nota mínima
python cartelera/cartelera_madrid.py --min-nota 7.0

# Filtrar por cine (solo funciona con --fuente sensacine)
python cartelera/cartelera_madrid.py --fuente sensacine --cine "Kinépolis"

# Forzar fuente concreta
python cartelera/cartelera_madrid.py --fuente ecartelera
python cartelera/cartelera_madrid.py --fuente sensacine

# Usar perfil de usuario personalizado
python cartelera/cartelera_madrid.py --perfil cartelera/perfil_ejemplo.json

# Enviar por Telegram
python cartelera/cartelera_madrid.py --telegram

# Sin filtros (toda la cartelera)
python cartelera/cartelera_madrid.py --sin-filtro

# JSON para procesar
python cartelera/cartelera_madrid.py --json > cartelera.json
```

> **Nota sobre --cine:** ecartelera.com no devuelve los nombres de cines individuales
> en su vista principal, solo el número total de cines. Para filtrar por nombre de cine
> usa `--fuente sensacine`.

#### Diagnóstico si devuelve 0 películas

```bash
# Guarda el HTML renderizado en /tmp/ para inspeccionar selectores CSS
python cartelera/cartelera_scraper.py --debug-html ecartelera
python cartelera/cartelera_scraper.py --debug-html sensacine
```

### 4. API REST

```bash
source .env
python api_server.py --port 8026

# Pruebas:
curl "http://localhost:8026/health"
curl "http://localhost:8026/pelicula?titulo=Interstellar"
curl "http://localhost:8026/pelicula?titulo=Interstellar&campo=nota"
curl "http://localhost:8026/cartelera"
curl "http://localhost:8026/cartelera?min_nota=7.0"
```

> **Importante:** el endpoint `/cartelera` llama al pipeline completo
> (scraping + enriquecimiento IMDB). La respuesta tarda ~5 minutos
> porque consulta IMDB para cada película en paralelo (8 workers).
> Planifica los timeouts de los clientes en consecuencia.

#### Endpoints disponibles

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/health` | Estado del servidor |
| GET | `/pelicula?titulo=X[&campo=Y]` | Info de una película |
| GET | `/cartelera[?min_nota=Y&fuente=Z]` | Cartelera de Madrid enriquecida con IMDB |
| POST | `/chat` | Pipeline completo con LLM (body: `{"mensaje": "..."}`) |

### 5. Alexa Skill

1. Crea un skill en [developer.amazon.com](https://developer.amazon.com)
2. En **Interaction Model → JSON Editor**: pega el contenido de `alexa/interaction_model.json`
3. Crea una Lambda en AWS con `alexa/alexa_lambda.py` como `lambda_function.py`
4. Variable de entorno Lambda: `MOVIE_API_URL=http://TU_IP_TAILSCALE:8026`
5. Conecta la Lambda al skill

**Frases de ejemplo:**
- *"Alexa, abre películas info"*
- *"¿Cuál es la nota de Interstellar?"*
- *"¿Quién dirigió 2001?"*
- *"De qué va Dune"*
- *"Cuánto dura El Padrino"*

### 6. n8n — Cartelera semanal por email

El workflow `n8n/workflows/cartelera-email.json` envía automáticamente la cartelera
de Madrid por email todos los **lunes a las 9:00**.

#### Levantar n8n

```bash
docker compose up n8n -d
```

Accede en `http://localhost:5678` con usuario `admin` y contraseña `admin123`.

#### Importar el workflow

Si el workflow no aparece automáticamente:
1. En n8n: **Add workflow → Import from file**
2. Selecciona `n8n/workflows/cartelera-email.json`

#### Configurar credenciales de email (SMTP)

El workflow usa SMTP con contraseña de aplicación de Gmail:

1. Activa la **verificación en dos pasos** en tu cuenta Google:
   [myaccount.google.com/security](https://myaccount.google.com/security)
2. Genera una contraseña de aplicación en:
   [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. En n8n, edita el nodo **Send an Email** y configura la credencial SMTP:
   - **Host:** `smtp.gmail.com`
   - **Port:** `587`
   - **SSL/TLS:** desactivado
   - **Disable STARTTLS:** desactivado
   - **User:** tu email de Gmail
   - **Password:** la contraseña de 16 caracteres generada

> **Nota Docker:** si aparece el error `self-signed certificate in certificate chain`,
> asegúrate de que el `docker-compose.yml` tiene estas variables en el servicio n8n:
> ```yaml
> NODE_TLS_REJECT_UNAUTHORIZED: "0"
> N8N_SSL_ENABLED: "false"
> ```

#### Pipeline del workflow

```
Schedule Trigger (lunes 9:00)
        ↓
HTTP Request → http://movie-agent-api:8026/cartelera
        ↓
Code JavaScript (construye tabla HTML con nota, director, sinopsis)
        ↓
Send an Email (SMTP Gmail)
```

## Perfil de usuario (cartelera)

Crea tu propio perfil basado en `cartelera/perfil_ejemplo.json`:

```json
{
  "generos": {
    "Ciencia ficción": 6.0,
    "Drama": 7.0,
    "Comedia": 5.5,
    "Terror": 6.0,
    "Acción": 6.5
  },
  "directores_favoritos": ["Christopher Nolan", "Denis Villeneuve"],
  "nota_minima_global": 5.0
}
```

Y úsalo con:

```bash
python cartelera/cartelera_madrid.py --perfil mi_perfil.json
```

**Reglas de filtrado (en orden de prioridad):**
1. Director favorito → siempre incluida, sin importar la nota
2. Sin nota IMDB + `--min-nota` explícita → excluida
3. Sin nota IMDB + solo perfil por género → incluida (beneficio de la duda)
4. Género en el perfil → se aplica la nota mínima de ese género
5. Género no en el perfil → se aplica `nota_minima_global`

## Cron automático (lunes 9:00)

### Con Docker (recomendado)

```bash
crontab -e
# Añadir:
0 9 * * 1 cd /ruta/al/proyecto && \
    docker compose run --rm cartelera \
    python cartelera/cartelera_madrid.py --telegram \
    --perfil cartelera/perfil_ejemplo.json \
    >> /var/log/cartelera.log 2>&1
```

### Sin Docker

```bash
crontab -e
# Añadir:
0 9 * * 1 cd /ruta/al/proyecto && source .env && \
    python cartelera/cartelera_madrid.py --telegram \
    >> /tmp/cartelera.log 2>&1
```

> El workflow de n8n también implementa esta automatización vía su Schedule Trigger,
> como alternativa al cron del sistema.

## Caché

El scraper guarda los resultados en `scraper/cache.json`.
Si una película ya fue consultada, se devuelven los datos guardados sin volver a hacer scraping.

```bash
# Limpiar toda la caché
rm scraper/cache.json

# Ignorar caché para una consulta concreta
python scraper/movie_scraper.py "Interstellar" --no-cache
```

## Tecnologías

| Componente | Tecnología |
|-----------|-----------|
| Frontend usuario | Telegram Bot, Alexa Skill, CLI, Email |
| Automatización | n8n (workflow semanal) |
| Orquestador | Python 3.12 |
| LLM local | Ollama (qwen2.5-coder:7b) |
| Scraping cartelera | Playwright (Chromium headless) |
| Fuente películas | IMDB |
| Fuente cartelera | eCartelera.com + SensaCine.com (fallback) |
| API bridge | HTTP server nativo Python |
| Caché | JSON local |
| Automatización sistema | cron |
| Contenedores | Docker + Docker Compose |
| Base de datos n8n | PostgreSQL 15 |
| VPN para Alexa | Tailscale |
