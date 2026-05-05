/**
 * app.js — CineBot frontend
 *
 * Tres modos de uso:
 *   1. Chat libre   → POST /chat         (pipeline completo: LLM → intención → scraping/API)
 *   2. Buscar película → GET /pelicula?titulo=X[&campo=Y]   (API REST directa)
 *   3. Cartelera    → GET /cartelera?[cine=X][&min_nota=Y][&fuente=Z]
 *
 * La URL base de la API se configura con el botón ⚙ (guardado en localStorage).
 * Default: http://localhost:8026
 */

'use strict';

// ── Configuración ────────────────────────────────────────
const DEFAULT_API = 'http://localhost:8026';
let API_BASE = localStorage.getItem('cinebot_api_url') || DEFAULT_API;

// Mostrar URL activa en footer
document.getElementById('api-url-display').textContent = API_BASE;

// ── Helpers de fetch ─────────────────────────────────────

async function apiFetch(path, { method = 'GET', body = null } = {}) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
  };
  if (body) opts.body = JSON.stringify(body);

  const res = await fetch(`${API_BASE}${path}`, opts);
  const data = await res.json();

  if (!res.ok) {
    throw new Error(data.error || data.detail || `HTTP ${res.status}`);
  }
  return data;
}

// ── TABS ─────────────────────────────────────────────────

document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.tab;

    document.querySelectorAll('.tab-btn').forEach(b => {
      b.classList.remove('active');
      b.setAttribute('aria-selected', 'false');
    });
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));

    btn.classList.add('active');
    btn.setAttribute('aria-selected', 'true');
    document.getElementById(`tab-${target}`).classList.add('active');
  });
});

// ── TAB 1: CHAT LIBRE ────────────────────────────────────

const chatWindow = document.getElementById('chat-window');
const chatForm   = document.getElementById('chat-form');
const chatInput  = document.getElementById('chat-input');

// Quick example chips
document.querySelectorAll('.example-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    chatInput.value = chip.dataset.text;
    chatInput.focus();
  });
});

chatForm.addEventListener('submit', async e => {
  e.preventDefault();
  const text = chatInput.value.trim();
  if (!text) return;

  appendMessage('user', text);
  chatInput.value = '';

  const typingEl = appendTyping();

  try {
    // Llamar al endpoint /chat del api_server
    // El servidor pasa la pregunta por el pipeline LLM → intención → scraper/cartelera
    const data = await apiFetch('/chat', { method: 'POST', body: { mensaje: text } });
    typingEl.remove();
    appendMessage('bot', formatChatResponse(data));
  } catch (err) {
    typingEl.remove();
    appendMessage('bot', `❌ ${err.message}`);
  }
});

function appendMessage(role, content) {
  const wrap = document.createElement('div');
  wrap.className = `chat-message ${role}`;

  const avatar = document.createElement('span');
  avatar.className = 'msg-avatar';
  avatar.textContent = role === 'bot' ? '🎬' : '👤';

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';

  // Si el contenido es HTML (para respuestas ricas del bot) o texto plano
  if (role === 'bot' && typeof content === 'string' && content.includes('<')) {
    bubble.innerHTML = content;
  } else {
    bubble.textContent = content;
  }

  wrap.appendChild(avatar);
  wrap.appendChild(bubble);
  chatWindow.appendChild(wrap);
  chatWindow.scrollTop = chatWindow.scrollHeight;
  return wrap;
}

function appendTyping() {
  const wrap = document.createElement('div');
  wrap.className = 'chat-message bot typing';

  const avatar = document.createElement('span');
  avatar.className = 'msg-avatar';
  avatar.textContent = '🎬';

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';
  bubble.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';

  wrap.appendChild(avatar);
  wrap.appendChild(bubble);
  chatWindow.appendChild(wrap);
  chatWindow.scrollTop = chatWindow.scrollHeight;
  return wrap;
}

/**
 * Formatea la respuesta del endpoint /chat en HTML para el chat bubble.
 * El servidor puede devolver:
 *   { tipo: 'pelicula', datos: {...} }
 *   { tipo: 'cartelera', datos: [...] }
 *   { tipo: 'texto', mensaje: '...' }
 *   { error: '...' }
 */
function formatChatResponse(data) {
  if (data.error) return `❌ ${data.error}`;

  if (data.tipo === 'texto' || data.mensaje) {
    return data.mensaje || data.texto || JSON.stringify(data);
  }

  if (data.tipo === 'pelicula' && data.datos) {
    return buildPeliculaHTML(data.datos);
  }

  if (data.tipo === 'cartelera' && Array.isArray(data.datos)) {
    if (data.datos.length === 0) return '📭 No encontré películas en cartelera con esos criterios.';
    return data.datos.slice(0, 5).map(p => buildCarteleraItemHTML(p)).join('') +
      (data.datos.length > 5 ? `<p style="color:var(--muted);font-size:0.8rem;margin-top:0.5rem">...y ${data.datos.length - 5} más. Ve a la pestaña Cartelera para verlas todas.</p>` : '');
  }

  // Respuesta genérica: mostrar raw JSON formateado
  return `<pre style="font-size:0.78rem;overflow-x:auto;white-space:pre-wrap">${JSON.stringify(data, null, 2)}</pre>`;
}

// ── TAB 2: BUSCAR PELÍCULA ───────────────────────────────

const searchForm   = document.getElementById('search-form');
const searchInput  = document.getElementById('search-input');
const searchResult = document.getElementById('search-result');

searchForm.addEventListener('submit', async e => {
  e.preventDefault();
  const titulo = searchInput.value.trim();
  if (!titulo) return;

  const campo = searchForm.querySelector('input[name="campo"]:checked')?.value || '';

  searchResult.innerHTML = '<div class="loading-msg">⟳ Buscando película...</div>';

  try {
    const params = new URLSearchParams({ titulo });
    if (campo) params.set('campo', campo);

    const data = await apiFetch(`/pelicula?${params}`);
    searchResult.innerHTML = '';

    if (campo && data[campo] !== undefined) {
      searchResult.appendChild(buildSingleFieldEl(titulo, campo, data[campo]));
    } else {
      searchResult.appendChild(buildMovieCardEl(data));
    }
  } catch (err) {
    searchResult.innerHTML = `<div class="error-msg">${err.message}</div>`;
  }
});

// ── TAB 3: CARTELERA ─────────────────────────────────────

const carteleraBtn    = document.getElementById('cartelera-btn');
const carteleraResult = document.getElementById('cartelera-result');
const cineFilter      = document.getElementById('cine-filter');
const minNotaInput    = document.getElementById('min-nota');
const fuenteSelect    = document.getElementById('fuente-select');

carteleraBtn.addEventListener('click', async () => {
  carteleraBtn.disabled = true;
  carteleraResult.innerHTML = '<div class="loading-msg">⟳ Descargando cartelera de Madrid... (puede tardar ~30s)</div>';

  try {
    const params = new URLSearchParams();
    const cine    = cineFilter.value.trim();
    const minNota = minNotaInput.value.trim();
    const fuente  = fuenteSelect.value;

    if (cine)    params.set('cine', cine);
    if (minNota) params.set('min_nota', minNota);
    if (fuente && fuente !== 'auto') params.set('fuente', fuente);

    const data = await apiFetch(`/cartelera?${params}`);
    carteleraResult.innerHTML = '';

    const peliculas = Array.isArray(data) ? data : data.peliculas || [];

    if (peliculas.length === 0) {
      carteleraResult.innerHTML = `
        <div class="empty-msg">
          <span class="empty-icon">📭</span>
          No encontré películas con los filtros aplicados.
        </div>`;
      return;
    }

    const grid = document.createElement('div');
    grid.className = 'cartelera-grid';
    peliculas.forEach(p => grid.appendChild(buildCarteleraCardEl(p)));
    carteleraResult.appendChild(grid);

  } catch (err) {
    carteleraResult.innerHTML = `<div class="error-msg">${err.message}</div>`;
  } finally {
    carteleraBtn.disabled = false;
  }
});

// ── BUILDERS DE UI ───────────────────────────────────────

/** Tarjeta completa de película (tab buscar) */
function buildMovieCardEl(data) {
  const card = document.createElement('div');
  card.className = 'movie-card';

  const fuente = data._fuente || '';
  const fuenteClass = fuente === 'playwright_scraping' ? 'scraping'
                    : fuente === 'omdb_api'             ? 'api'
                    : 'cache';
  const fuenteLabel = fuente === 'playwright_scraping' ? '🌐 scraping web'
                    : fuente === 'omdb_api'             ? '📡 OMDB API'
                    : fuente === 'cache'                ? '💾 caché'
                    : '';

  card.innerHTML = `
    <div class="movie-card-title">${esc(data.titulo || '—')}</div>
    ${fuenteLabel ? `<div class="movie-card-source ${fuenteClass}">${fuenteLabel}</div>` : ''}
    <div class="movie-stats">
      ${data.nota     ? `<div class="stat"><span class="stat-icon">⭐</span><span class="stat-value nota-value">${data.nota}/10</span></div>` : ''}
      ${data.votos    ? `<div class="stat"><span class="stat-icon">🗳</span><span class="stat-value">${fmtVotos(data.votos)} votos</span></div>` : ''}
      ${data.director ? `<div class="stat"><span class="stat-icon">🎭</span><span class="stat-value">${esc(data.director)}</span></div>` : ''}
      ${data.duracion ? `<div class="stat"><span class="stat-icon">⏱</span><span class="stat-value">${data.duracion} min</span></div>` : ''}
    </div>
    ${data.sinopsis ? `
      <div class="movie-synopsis">
        <div class="movie-synopsis-label">Sinopsis</div>
        ${esc(data.sinopsis)}
      </div>` : ''}
    ${data.url_imdb ? `<a class="imdb-link" href="${data.url_imdb}" target="_blank" rel="noopener">↗ Ver en IMDB</a>` : ''}
  `;
  return card;
}

/** Resultado de campo único (tab buscar con campo seleccionado) */
function buildSingleFieldEl(titulo, campo, valor) {
  const el = document.createElement('div');
  el.className = 'single-field-result';

  const isLong = typeof valor === 'string' && valor.length > 40;
  el.innerHTML = `
    <div class="single-field-label">${esc(titulo)} · ${campo}</div>
    <div class="single-field-value ${isLong ? 'long-text' : ''}">${esc(String(valor ?? '—'))}</div>
  `;
  return el;
}

/** Tarjeta de cartelera (tab cartelera) */
function buildCarteleraCardEl(p) {
  const card = document.createElement('div');
  card.className = 'cartelera-card';

  const cinesStr = p.cines?.length
    ? p.cines.slice(0, 3).join(', ') + (p.cines.length > 3 ? ` +${p.cines.length - 3}` : '')
    : null;

  const fuente = p._fuente === 'ecartelera' ? 'eCartelera' : p._fuente === 'sensacine' ? 'Sensacine' : '';

  card.innerHTML = `
    <div class="cartelera-card-title">${esc(p.titulo)}</div>
    ${p.nota_imdb != null ? `<div class="cartelera-nota">⭐ ${p.nota_imdb}</div>` : ''}
    <div class="cartelera-meta">
      ${p.genero     ? `Género: <span>${esc(p.genero)}</span><br>` : ''}
      ${p.director   ? `Director: <span>${esc(p.director)}</span><br>` : ''}
      ${p.duracion_min != null ? `Duración: <span>${p.duracion_min} min</span>` : ''}
    </div>
    ${p.sinopsis ? `<div class="cartelera-sinopsis">${esc(p.sinopsis)}</div>` : ''}
    ${cinesStr ? `<div class="cartelera-cines">🏛 ${esc(cinesStr)}</div>` : ''}
    ${fuente ? `<div class="cartelera-fuente">${fuente}</div>` : ''}
  `;
  return card;
}

/** HTML compacto para el chat bubble (respuesta de tipo película) */
function buildPeliculaHTML(d) {
  const stats = [
    d.nota     ? `⭐ ${d.nota}` : null,
    d.director ? `🎭 ${esc(d.director)}` : null,
    d.duracion ? `⏱ ${d.duracion} min` : null,
    d.votos    ? `🗳 ${fmtVotos(d.votos)} votos` : null,
  ].filter(Boolean).join('  ·  ');

  const sinopsis = d.sinopsis
    ? `<p style="margin-top:0.5rem;color:var(--cream-dim);font-size:0.88rem">${esc(d.sinopsis.slice(0, 250))}${d.sinopsis.length > 250 ? '…' : ''}</p>`
    : '';

  const link = d.url_imdb
    ? `<a href="${d.url_imdb}" target="_blank" rel="noopener" style="font-size:0.78rem;color:var(--amber-dim);text-decoration:underline">Ver en IMDB ↗</a>`
    : '';

  return `
    <strong style="font-family:var(--font-display);font-size:1.15rem;color:var(--amber)">${esc(d.titulo)}</strong>
    <p style="font-size:0.82rem;color:var(--cream-dim);margin:0.25rem 0">${stats}</p>
    ${sinopsis}
    ${link}
  `;
}

/** HTML compacto para el chat bubble (item de cartelera) */
function buildCarteleraItemHTML(p) {
  return `
    <div style="border-bottom:1px solid var(--border);padding-bottom:0.6rem;margin-bottom:0.6rem">
      <strong style="font-family:var(--font-display);font-size:1.05rem;color:var(--cream)">${esc(p.titulo)}</strong>
      ${p.nota_imdb != null ? ` <span style="color:var(--amber);font-size:0.82rem">⭐ ${p.nota_imdb}</span>` : ''}
      ${p.director ? `<span style="color:var(--muted);font-size:0.8rem"> · 🎭 ${esc(p.director)}</span>` : ''}
    </div>
  `;
}

// ── UTILIDADES ───────────────────────────────────────────

function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function fmtVotos(n) {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
  if (n >= 1_000)     return (n / 1_000).toFixed(0) + 'K';
  return String(n);
}

// ── MODAL DE CONFIGURACIÓN ───────────────────────────────

const gearBtn       = document.getElementById('gear-btn');
const modalBackdrop = document.getElementById('modal-backdrop');
const modalApiUrl   = document.getElementById('modal-api-url');
const modalSave     = document.getElementById('modal-save');
const modalCancel   = document.getElementById('modal-cancel');

function openModal() {
  modalApiUrl.value = API_BASE;
  modalBackdrop.hidden = false;
  modalApiUrl.focus();
}

function closeModal() {
  modalBackdrop.hidden = true;
}

gearBtn.addEventListener('click', openModal);
modalCancel.addEventListener('click', closeModal);
modalBackdrop.addEventListener('click', e => { if (e.target === modalBackdrop) closeModal(); });

modalSave.addEventListener('click', () => {
  const url = modalApiUrl.value.trim().replace(/\/$/, '');
  if (!url) return;
  API_BASE = url;
  localStorage.setItem('cinebot_api_url', url);
  document.getElementById('api-url-display').textContent = url;
  closeModal();
});

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
