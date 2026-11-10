/*
 * netflix-cec — script inyectado en la página de Netflix (DevTools en Chromium, WebDriver BiDi en Firefox).
 * Expone window.__nfx.cmd(nombre, args) para que el servicio lo use.
 * __NFX_VERSION__ y __NFX_CONFIG__ los reemplaza el servicio al inyectar.
 */
(() => {
  'use strict';
  if (window.top !== window) return;
  const V = "__NFX_VERSION__";
  if (window.__nfx && window.__nfx.v === V) return;
  if (window.__nfx && typeof window.__nfx.destroy === 'function') { try { window.__nfx.destroy(); } catch (e) { /* nada */ } }
  const C = __NFX_CONFIG__;

  const S = {
    cur: null, lastRect: null, beforeModal: null, path: location.pathname,
    raf: 0, ringKey: '', toastTimer: 0, seekT: null, seekAt: 0,
    pagingRow: null, pagingIndex: -1, navMode: false, navModePath: '',
    busyTimer: 0, echoTimer: 0, staleAt: 0, followUntil: 0, tickTimer: 0, listeners: [],
  };
  // Escuchas que hay que sacar en destroy() (si no, una versión vieja del script sigue reaccionando).
  function on(target, type, fn, opts) { target.addEventListener(type, fn, opts); S.listeners.push([target, type, fn, opts]); }

  // ------------------------------------------------------------ utilidades
  const $$ = (sel, root) => { if (!sel) return []; try { return Array.from((root || document).querySelectorAll(sel)); } catch (e) { return []; } };
  const matches = (el, sel) => { if (!sel || !el || !el.matches) return false; try { return el.matches(sel); } catch (e) { return false; } };
  const closest = (el, sel) => { if (!sel || !el || !el.closest) return null; try { return el.closest(sel); } catch (e) { return null; } };
  const vw = () => window.innerWidth;
  const vh = () => window.innerHeight;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const inPlayer = () => location.pathname.startsWith('/watch');
  const textOf = (el) => ((el && (el.getAttribute('aria-label') || el.innerText || el.textContent || el.getAttribute('title'))) || '')
    .replace(/\s+/g, ' ').trim().slice(0, 80);
  const TEXT_SEL = 'input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]):not([type="range"]), textarea, [contenteditable=""], [contenteditable="true"]';
  const scrollBehavior = () => (C.smooth_scroll ? 'smooth' : 'auto');

  function R(el) {
    const r = el.getBoundingClientRect();
    return { l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height, cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
  }

  function cssVisible(el) {
    if (el.checkVisibility) {
      return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true, opacityProperty: true, visibilityProperty: true });
    }
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity) > 0.02;
  }

  // ¿Se puede enfocar? (visible, mayormente dentro de la pantalla en horizontal)
  function usable(el, opts) {
    opts = opts || {};
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    // Lejos de la pantalla (otras filas del inicio): no hace falta revisar si se ve, que es lo caro.
    if (opts.near && (r.bottom < -vh() * 1.5 || r.top > vh() * 2.5)) return false;
    const visW = Math.min(r.right, vw()) - Math.max(r.left, 0);
    if (visW < Math.min(r.width, vw()) * 0.6) return false;
    if (opts.inView) {
      const visH = Math.min(r.bottom, vh()) - Math.max(r.top, 0);
      if (visH < Math.min(r.height, vh()) * 0.5) return false;
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    if (C.respect_aria_hidden && closest(el, '[aria-hidden="true"], [inert]')) return false;
    return cssVisible(el);
  }

  function anyShown(sel, root) {
    return $$(sel, root).some((e) => {
      const r = e.getBoundingClientRect();
      return r.width > 2 && r.height > 2 && cssVisible(e) && !closest(e, '[aria-hidden="true"]');
    });
  }

  // Candidatos a foco: se quedan solo las "hojas" (si un contenedor enfocable
  // tiene adentro otro enfocable, se usa el de adentro).
  function candidates(root, opts) {
    root = root || document;
    opts = Object.assign({ near: true }, opts);
    const all = $$(C.focusable, root).filter((el) => !matches(el, C.ignore) && usable(el, opts));
    const set = new Set(all);
    const parents = new Set();
    for (const el of all) {
      let p = el.parentElement;
      while (p && p !== root) { if (set.has(p)) parents.add(p); p = p.parentElement; }
    }
    return all.filter((el) => !parents.has(el));
  }

  function modalRoot() {
    const ms = $$(C.modal).filter((m) => {
      const r = m.getBoundingClientRect();
      return r.width > 80 && r.height > 80 && cssVisible(m) && !(C.respect_aria_hidden && closest(m, '[aria-hidden="true"]'));
    });
    for (let i = ms.length - 1; i >= 0; i--) { if (candidates(ms[i]).length) return ms[i]; }
    return null;
  }

  const inHeader = (el) => !!closest(el, C.header);

  function headerBottom() {
    let b = 0;
    for (const h of $$(C.header)) {
      const pos = getComputedStyle(h).position;
      if (pos !== 'fixed' && pos !== 'sticky') continue;
      const r = h.getBoundingClientRect();
      if (r.top <= 1 && r.height > 0 && r.height < vh() * 0.4) b = Math.max(b, r.bottom);
    }
    return b;
  }

  // ------------------------------------------------------------ resaltado
  function ensureStyle() {
    if (document.getElementById('nfx-style')) return;
    const st = document.createElement('style');
    st.id = 'nfx-style';
    st.textContent = `
#nfx-ring{position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;z-index:2147483647;box-sizing:border-box;
  border:4px solid #fff;border-radius:8px;box-shadow:0 0 0 3px #e50914,0 0 24px 6px rgba(229,9,20,.7);
  opacity:0;transition:transform .12s ease-out,width .12s ease-out,height .12s ease-out,opacity .15s}
#nfx-ring.on{opacity:1}
#nfx-toast{position:fixed;left:50%;bottom:9vh;transform:translate(-50%,12px);max-width:80vw;box-sizing:border-box;
  padding:.5em 1.1em;border-radius:10px;border-left:6px solid #e50914;background:rgba(20,20,20,.9);color:#fff;
  font:600 clamp(18px,2.1vw,42px)/1.3 "Netflix Sans",Helvetica,Arial,sans-serif;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;z-index:2147483647;pointer-events:none;opacity:0;box-shadow:0 6px 24px rgba(0,0,0,.55);
  transition:opacity .18s,transform .18s}
#nfx-toast.on{opacity:1;transform:translate(-50%,0)}
#nfx-busy{position:fixed;top:4vh;right:3vw;display:flex;align-items:center;gap:.6em;max-width:60vw;box-sizing:border-box;
  padding:.45em 1.1em .45em .75em;border-radius:999px;background:rgba(20,20,20,.9);color:#fff;
  font:600 clamp(16px,1.6vw,34px)/1.3 "Netflix Sans",Helvetica,Arial,sans-serif;white-space:nowrap;overflow:hidden;
  z-index:2147483647;pointer-events:none;opacity:0;transition:opacity .15s;box-shadow:0 4px 18px rgba(0,0,0,.55)}
#nfx-busy.on{opacity:1}
#nfx-busy span{overflow:hidden;text-overflow:ellipsis}
#nfx-busy i{flex:none;width:1em;height:1em;box-sizing:border-box;border-radius:50%;border:.17em solid rgba(255,255,255,.25);
  border-top-color:#e50914;animation:nfx-spin .8s linear infinite!important;animation-duration:.8s!important;
  animation-iteration-count:infinite!important}
@keyframes nfx-spin{to{transform:rotate(360deg)}}
#nfx-echo{position:fixed;top:4vh;left:3vw;min-width:1.6em;padding:.25em .6em;box-sizing:border-box;border-radius:8px;
  background:rgba(20,20,20,.85);color:#fff;text-align:center;font:700 clamp(16px,1.6vw,34px)/1.3 Helvetica,Arial,sans-serif;
  z-index:2147483647;pointer-events:none;opacity:0;transition:opacity .1s}
#nfx-echo.on{opacity:1}`;
    (document.head || document.documentElement).appendChild(st);
  }

  // En pantalla completa solo se ve lo que está dentro del elemento a pantalla completa.
  function overlay(id) {
    let e = document.getElementById(id);
    if (!e) { e = document.createElement('div'); e.id = id; }
    const host = document.fullscreenElement || document.body || document.documentElement;
    if (e.parentNode !== host) host.appendChild(e);
    return e;
  }

  function positionRing() {
    const ring = overlay('nfx-ring');
    if (S.cur && !S.cur.isConnected) {
      // El elemento enfocado se re-renderizó: tomar el que quedó en su lugar.
      const rec = recover(modalRoot() || document);
      if (rec.el && rec.near) { S.cur = rec.el; S.cur.classList.add('nfx-focused'); } else { S.cur = null; }
    }
    if (!S.cur) { ring.classList.remove('on'); S.ringKey = ''; return false; }
    const r = S.cur.getBoundingClientRect();
    const pad = 6;
    const key = [r.left, r.top, r.width, r.height].map(Math.round).join(',');
    if (key !== S.ringKey) {
      S.ringKey = key;
      ring.style.transform = `translate(${Math.round(r.left - pad)}px,${Math.round(r.top - pad)}px)`;
      ring.style.width = `${Math.round(r.width + 2 * pad)}px`;
      ring.style.height = `${Math.round(r.height + 2 * pad)}px`;
    }
    ring.classList.add('on');
    return true;
  }

  // El marco puede quedar "colgado" sin que se apriete ninguna tecla: se abrió una ficha encima del
  // elemento resaltado, o el elemento se ocultó sin salir de la página. Se revisa unas veces por segundo
  // mientras el marco está visible.
  function staleCheck() {
    const now = performance.now();
    if (!S.cur || !S.cur.isConnected || now - S.staleAt < 150) return;
    S.staleAt = now;
    const modal = modalRoot();
    if (modal && !modal.contains(S.cur)) {
      // Ficha abierta encima: el resaltado pasa a su botón principal (al cerrarla vuelve a la tarjeta).
      if (!S.beforeModal) S.beforeModal = S.cur;
      S.lastRect = null;
      setCur(initial(modal) || null, false);
      return;
    }
    const r = S.cur.getBoundingClientRect();
    const hidden = r.width < 4 || r.height < 4 || !cssVisible(S.cur)
      || (C.respect_aria_hidden && !!closest(S.cur, '[aria-hidden="true"], [inert]'));
    if (hidden) {
      const rec = recover(modal || document);
      setCur(rec.el && rec.near && rec.el !== S.cur ? rec.el : null, false);
    }
  }

  // El marco sigue al elemento cuadro a cuadro solo un rato después de que algo se mueve (una tecla, un
  // scroll, un cambio de tamaño). El resto del tiempo se revisa 4 veces por segundo: un
  // requestAnimationFrame permanente obligaría al navegador a recalcular la página en cada cuadro.
  function ringLoop() {
    S.raf = 0;
    if (document.hidden) return;
    if (positionRing() && performance.now() < S.followUntil) S.raf = requestAnimationFrame(ringLoop);
  }

  function follow(ms) {
    if (!S.cur) return;
    S.followUntil = Math.max(S.followUntil, performance.now() + (ms || 400));
    if (!S.raf) S.raf = requestAnimationFrame(ringLoop);
  }

  function ringTick() {
    if (document.hidden) return;
    if (!S.cur) { clearInterval(S.tickTimer); S.tickTimer = 0; return; }
    staleCheck();          // puede mover el resaltado o apagarlo
    if (S.cur) positionRing();  // corrimientos sin scroll (imágenes que terminan de cargar, filas nuevas)
  }

  function showRing() {
    ensureStyle(); positionRing(); follow(400);
    if (!S.tickTimer) S.tickTimer = setInterval(ringTick, 250);
  }

  function hideRing() {
    const ring = document.getElementById('nfx-ring');
    if (ring) ring.classList.remove('on');
    S.ringKey = '';
    cancelAnimationFrame(S.raf); S.raf = 0;
    clearInterval(S.tickTimer); S.tickTimer = 0;
  }

  function toast(text, ms) {
    ensureStyle();
    const t = overlay('nfx-toast');
    t.textContent = String(text);
    void t.offsetWidth;
    t.classList.add('on');
    clearTimeout(S.toastTimer);
    S.toastTimer = setTimeout(() => t.classList.remove('on'), ms || 1800);
    return { ok: true };
  }

  // ------------------------------------------------------------ indicador "trabajando"
  // Píldora con un circulito girando mientras el servicio hace algo que tarda. Con "until" se guarda en
  // sessionStorage y sobrevive a la navegación: el script la vuelve a mostrar en la página nueva y la
  // apaga solo cuando se cumple la condición (o vence).
  const BUSY_KEY = 'nfx-busy';
  function busyLoad() {
    try { return JSON.parse(sessionStorage.getItem(BUSY_KEY) || 'null'); } catch (e) { return null; }
  }
  function busyStore(v) {
    try { if (v) sessionStorage.setItem(BUSY_KEY, JSON.stringify(v)); else sessionStorage.removeItem(BUSY_KEY); } catch (e) { /* nada */ }
  }
  function whenBody(fn) {
    if (document.body) { fn(); return; }
    const mo = new MutationObserver(() => { if (document.body) { mo.disconnect(); fn(); } });
    mo.observe(document.documentElement || document, { childList: true, subtree: true });
  }
  function busyShow(text) {
    whenBody(() => {
      ensureStyle();
      const b = overlay('nfx-busy');
      if (!b.firstChild) b.innerHTML = '<i></i><span></span>';
      b.lastChild.textContent = String(text);
      b.classList.add('on');
    });
  }
  function busyHide() {
    const b = document.getElementById('nfx-busy');
    if (b) b.classList.remove('on');
    clearInterval(S.busyTimer); S.busyTimer = 0;
  }
  function videoPlaying() {
    const v = inPlayer() && document.querySelector('video');
    return !!v && !v.paused && v.readyState >= 3 && v.currentTime > 0;
  }
  // ¿Ya está lo que se esperaba? playing = el video arrancó; page = Netflix terminó de cargar algo usable.
  function busyReached(st) {
    if (st.nav && performance.timeOrigin === st.doc) return false;  // todavía es la página anterior
    if (st.until === 'playing') return videoPlaying();
    if (st.until === 'profiles') return ctx().ctx === 'profiles';
    if (st.until === 'page') {
      const c = ctx().ctx;
      return c === 'profiles' || videoPlaying() || (c === 'browse' && (anyShown(C.row) || !!modalRoot()));
    }
    return false;
  }
  function busyWatch(st) {
    clearInterval(S.busyTimer);
    S.busyTimer = setInterval(() => {
      if (Date.now() > st.exp || (st.until && busyReached(st))) { busyHide(); if (st.until) busyStore(null); }
    }, 250);
  }
  function busy(text, until, ttl, nav) {
    const st = { text: String(text), until: until || null, exp: Date.now() + (ttl || 15000), nav: !!nav, doc: performance.timeOrigin };
    busyStore(st.until ? st : null);
    busyShow(st.text);
    busyWatch(st);
    return { ok: true, url: location.href };
  }
  function done(text, ms) {
    busyHide(); busyStore(null);
    if (text) toast(text, ms);
    return { ok: true };
  }
  function busyResume() {
    const st = busyLoad();
    if (!st) return;
    if (Date.now() > st.exp) { busyStore(null); return; }
    busyShow(st.text);
    busyWatch(st);
  }

  // Eco de teclas (diagnóstico): muestra un instante la tecla que llegó.
  function echo(label) {
    whenBody(() => {
      ensureStyle();
      const e = overlay('nfx-echo');
      e.textContent = String(label);
      e.classList.add('on');
      clearTimeout(S.echoTimer);
      S.echoTimer = setTimeout(() => e.classList.remove('on'), 600);
    });
    return { ok: true };
  }

  // ------------------------------------------------------------ foco
  function scrollToShow(el) {
    const r = el.getBoundingClientRect();
    const top = headerBottom();
    const bottom = vh();
    const margin = Math.min(120, (bottom - top) * 0.12);
    if (r.top >= top + margin && r.bottom <= bottom - margin) return;
    if (r.height > bottom - top - 2 * margin && r.top >= top && r.top < bottom) return;
    const delta = Math.round((r.top + r.height / 2) - (top + (bottom - top) / 2));
    const behavior = scrollBehavior();
    // No se usa scrollIntoView: desplazaría las filas de Netflix (overflow:hidden) de costado.
    let p = el.parentElement;
    while (p && p !== document.body && p !== document.documentElement) {
      const oy = getComputedStyle(p).overflowY;
      if (/(auto|scroll|overlay)/.test(oy) && p.scrollHeight > p.clientHeight + 4) { p.scrollBy({ top: delta, behavior }); return; }
      p = p.parentElement;
    }
    window.scrollBy({ top: delta, behavior });
  }

  function setCur(el, scroll) {
    if (S.cur && S.cur !== el && S.cur.classList) S.cur.classList.remove('nfx-focused');
    S.cur = el || null;
    if (!el) { hideRing(); return; }
    el.classList.add('nfx-focused');
    if (C.native_focus) { try { el.focus({ preventScroll: true }); } catch (e) { /* nada */ } }
    if (scroll !== false) scrollToShow(el);
    S.lastRect = R(el);
    showRing();
  }

  function valid(el, root) {
    return !!el && el.isConnected && (root === document || root.contains(el)) && !matches(el, C.ignore) && usable(el);
  }

  function initial(root, cands) {
    for (const sel of (C.initial || [])) {
      const el = $$(sel, root).find((e) => !matches(e, C.ignore) && usable(e, { inView: true }));
      if (el) return el;
    }
    const list = (cands || candidates(root)).filter((e) => usable(e, { inView: true }));
    list.sort((a, b) => {
      const ha = inHeader(a); const hb = inHeader(b);
      if (ha !== hb) return ha ? 1 : -1;
      const ra = R(a); const rb = R(b);
      return (Math.round(ra.t / 40) - Math.round(rb.t / 40)) || (ra.l - rb.l);
    });
    return list[0] || null;
  }

  // Devuelve {el, near}: near = es el mismo elemento de antes o uno en la misma posición
  // (Netflix re-renderizó), así que se puede seguir moviendo desde ahí sin "perder" la tecla.
  function recover(root) {
    if (S.beforeModal && root === document && valid(S.beforeModal, root)) {
      const el = S.beforeModal; S.beforeModal = null; return { el, near: true };
    }
    const cands = candidates(root);
    if (S.lastRect && cands.length) {
      const L = S.lastRect; let best = null; let bd = Infinity;
      for (const c of cands) {
        const r = R(c);
        const d = Math.hypot(r.cx - L.cx, r.cy - L.cy);
        if (d < bd) { bd = d; best = c; }
      }
      const span = Math.max(vw(), vh());
      if (best && bd < span * 0.12) return { el: best, near: true };
      if (best && bd < span * 0.4) return { el: best, near: false };
    }
    return { el: initial(root, cands), near: false };
  }

  // Estado actual: raíz de navegación (modal o documento) y foco válido.
  function current() {
    if (S.path !== location.pathname) {
      S.path = location.pathname; S.lastRect = null; S.beforeModal = null;
      if (S.cur && !S.cur.isConnected) setCur(null);
    }
    const modal = modalRoot();
    const root = modal || document;
    if (modal && S.cur && !modal.contains(S.cur)) {
      // Se abrió un modal: recordar dónde estábamos y empezar por su botón principal.
      if (!S.beforeModal) S.beforeModal = S.cur;
      setCur(null);
      S.lastRect = null;
    }
    if (S.cur && !valid(S.cur, root)) setCur(null);
    return { root, modal };
  }

  function pick(from, cands, dir) {
    const horizontal = dir === 'left' || dir === 'right';
    let best = null; let bestScore = Infinity;
    for (const c of cands) {
      if (c === S.cur) continue;
      const r = R(c);
      let primary;
      if (dir === 'right') { if (r.cx <= from.cx + 1 || r.r <= from.r - 1) continue; primary = Math.max(0, r.l - from.r); }
      else if (dir === 'left') { if (r.cx >= from.cx - 1 || r.l >= from.l + 1) continue; primary = Math.max(0, from.l - r.r); }
      else if (dir === 'down') { if (r.cy <= from.cy + 1 || r.b <= from.b - 1) continue; primary = Math.max(0, r.t - from.b); }
      else { if (r.cy >= from.cy - 1 || r.t >= from.t + 1) continue; primary = Math.max(0, from.t - r.b); }
      let gap; let off;
      if (horizontal) { gap = Math.max(0, Math.max(r.t, from.t) - Math.min(r.b, from.b)); off = Math.abs(r.cy - from.cy); }
      else { gap = Math.max(0, Math.max(r.l, from.l) - Math.min(r.r, from.r)); off = Math.abs(r.cx - from.cx); }
      const score = primary + gap * (horizontal ? 5 : 2) + off * (horizontal ? 0.5 : 0.25);
      if (score < bestScore) { bestScore = score; best = c; }
    }
    return best;
  }

  function focusFirst(root) {
    const { el } = recover(root);
    setCur(el);
    return { focused: !!el, label: textOf(el) };
  }

  function restore() {
    const { root } = current();
    if (!S.cur) { const { el } = recover(root); if (el) setCur(el, false); }
    return { label: textOf(S.cur) };
  }

  function nav(dir) {
    const { root } = current();
    if (!S.cur) {
      const rec = recover(root);
      if (!rec.el || !rec.near) { setCur(rec.el); return { focused: !!rec.el, label: textOf(rec.el) }; }
      setCur(rec.el, false);
    }
    const from = R(S.cur);
    const horizontal = dir === 'left' || dir === 'right';
    const cands = candidates(root);

    // En una fila de títulos: moverse dentro de la fila y, al llegar al borde, pasar de página.
    const row = horizontal ? closest(S.cur, C.row) : null;
    if (row) {
      const best = pick(from, cands.filter((c) => row.contains(c)), dir);
      if (best) { setCur(best); return { moved: true, label: textOf(best) }; }
      const handle = $$(dir === 'right' ? C.row_next : C.row_prev, row).find((h) => h.getBoundingClientRect().width > 0);
      if (handle) {
        S.pagingRow = row; S.pagingIndex = $$(C.row).indexOf(row);
        try { handle.click(); } catch (e) { /* nada */ }
        return { paged: true };
      }
      return { edge: true };
    }

    let best = pick(from, cands, dir);
    // Al subir, la barra superior fija queda "encima" de todo: solo ir a ella si no hay otra cosa.
    if (best && dir === 'up' && inHeader(best) && !inHeader(S.cur)) {
      const alt = pick(from, cands.filter((c) => !inHeader(c)), dir);
      if (alt) best = alt;
    }
    if (best) { setCur(best); return { moved: true, label: textOf(best) }; }
    if (dir === 'down') { window.scrollBy({ top: vh() * 0.6, behavior: scrollBehavior() }); return { scrolled: true }; }
    if (dir === 'up' && root === document && window.scrollY > 0) { window.scrollTo({ top: 0, behavior: scrollBehavior() }); return { scrolled: true }; }
    return { edge: true };
  }

  function afterPage(dir) {
    let row = S.pagingRow;
    if (!row || !row.isConnected) row = $$(C.row)[S.pagingIndex] || null;
    S.pagingRow = null;
    if (!row) return focusFirst(document);
    const cands = candidates(row).filter((c) => { const r = c.getBoundingClientRect(); return r.left >= -2 && r.right <= vw() + 2; });
    if (!cands.length) return focusFirst(document);
    cands.sort((a, b) => R(a).l - R(b).l);
    const el = dir === 'right' ? cands[0] : cands[cands.length - 1];
    setCur(el);
    return { moved: true, label: textOf(el) };
  }

  // Punto para un clic "real" (vía DevTools). Si el centro está tapado, clic por JS.
  function target(el) {
    let r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > vh()) { scrollToShow(el); r = el.getBoundingClientRect(); }
    const x = Math.round(clamp(r.left + r.width / 2, 1, vw() - 2));
    const y = Math.round(clamp(r.top + r.height / 2, 1, vh() - 2));
    if (C.trusted_click) {
      const hit = document.elementFromPoint(x, y);
      if (hit && (hit === el || el.contains(hit))) return { x, y };
    }
    try { el.click(); } catch (e) { /* nada */ }
    return { clicked: true };
  }

  function activate() {
    const { root } = current();
    if (!S.cur) return focusFirst(root);
    const el = S.cur;
    return Object.assign(target(el), { label: textOf(el) });
  }

  function back() {
    const a = document.activeElement;
    if (a && a !== document.body && matches(a, TEXT_SEL)) { a.blur(); return { done: 'blur' }; }
    const { modal } = current();
    if (inPlayer()) {
      if (S.navMode) { S.navMode = false; setCur(null); toast('Modo reproducción'); return { done: 'navmode_off' }; }
      if (anyShown(C.player_menu)) return { key: 'Escape' };
      const b = $$(C.player_back).find((e) => e.isConnected);
      if (b) { try { b.click(); } catch (e) { /* nada */ } return { done: 'player_back' }; }
      if (history.length > 1) { history.back(); return { done: 'history' }; }
      location.href = '/browse';
      return { done: 'browse' };
    }
    if (modal) return { key: 'Escape', restore: true };
    if (/^\/browse\/?$/.test(location.pathname)) {
      if (window.scrollY > 10) {
        window.scrollTo({ top: 0, behavior: scrollBehavior() }); setCur(null); S.lastRect = null;
        return { done: 'top' };
      }
      return { done: 'none' };
    }
    if (history.length > 1) { history.back(); return { done: 'history' }; }
    location.href = '/browse';
    return { done: 'browse' };
  }

  // ------------------------------------------------------------ contexto
  function profileLinks() {
    const list = $$(C.profile_link).filter((e) => {
      if (matches(e, C.profile_exclude) || closest(e, C.profile_exclude)) return false;
      if (/manage|addprofile|add-profile|\/profiles\/(new|add)/i.test(e.getAttribute('href') || '')) return false;
      const r = e.getBoundingClientRect();
      return r.width > 10 && r.height > 10 && cssVisible(e) && !closest(e, '[aria-hidden="true"]');
    });
    return list.filter((e) => !list.some((o) => o !== e && o.contains(e)));
  }

  function ctx() {
    const a = document.activeElement;
    let c;
    if (a && a !== document.body && matches(a, TEXT_SEL)) c = 'text';
    else if (inPlayer()) {
      if (S.navMode && S.navModePath === location.pathname) c = 'player_menu';
      else { S.navMode = false; c = anyShown(C.player_menu) ? 'player_menu' : 'player'; }
      if (c === 'player' && S.cur) setCur(null);
    } else if (anyShown(C.profile_gate) || (profileLinks().length && !anyShown(C.row))) c = 'profiles';
    else c = 'browse';
    return { ctx: c, url: location.href, title: document.title };
  }

  // ------------------------------------------------------------ Netflix
  function npl() {
    try {
      const vp = window.netflix.appContext.state.playerApp.getAPI().videoPlayer;
      const ids = vp.getAllPlayerSessionIds() || [];
      const id = ids.find((i) => /^watch/.test(i)) || ids[ids.length - 1];
      return id ? vp.getVideoPlayerBySessionId(id) : null;
    } catch (e) { return null; }
  }
  const has = (p, m) => !!p && typeof p[m] === 'function';
  const fmt = (s) => {
    s = Math.max(0, Math.round(s));
    const h = Math.floor(s / 3600); const m = Math.floor((s % 3600) / 60); const x = s % 60;
    return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + ':' + String(x).padStart(2, '0');
  };

  function playPause(force) {
    const p = npl();
    if (has(p, 'isPaused') && has(p, 'play') && has(p, 'pause')) {
      const paused = p.isPaused();
      const play = force === 'play' ? true : force === 'pause' ? false : paused;
      if (play) p.play(); else p.pause();
      toast(play ? '▶  Reproducir' : '❚❚  Pausa', 900);
      return { state: play ? 'playing' : 'paused' };
    }
    const v = document.querySelector('video');
    if (force && v && (force === 'play') !== v.paused) return { state: 'unchanged' };
    return { fallback: 'key', key: ' ' };
  }

  function seek(sec) {
    const p = npl();
    if (has(p, 'seek') && has(p, 'getCurrentTime')) {
      const now = Date.now();
      // Pulsaciones seguidas se acumulan aunque el reproductor todavía no haya terminado de saltar.
      const base = (S.seekT !== null && now - S.seekAt < 1500) ? S.seekT : p.getCurrentTime();
      const dur = has(p, 'getDuration') ? p.getDuration() : Infinity;
      const t = clamp(base + sec * 1000, 0, Math.max(0, dur - 2000));
      p.seek(t); S.seekT = t; S.seekAt = now;
      toast(`${sec >= 0 ? '»' : '«'}  ${fmt(t / 1000)}${isFinite(dur) ? ' / ' + fmt(dur / 1000) : ''}`, 1200);
      return { t: t / 1000 };
    }
    return { fallback: 'key', key: sec >= 0 ? 'ArrowRight' : 'ArrowLeft', times: Math.max(1, Math.round(Math.abs(sec) / 10)) };
  }

  function volume(delta) {
    const p = npl();
    if (has(p, 'getVolume') && has(p, 'setVolume')) {
      const v = clamp(Math.round((p.getVolume() + delta) * 100) / 100, 0, 1);
      p.setVolume(v);
      if (v > 0 && has(p, 'isMuted') && has(p, 'setMuted') && p.isMuted()) p.setMuted(false);
      toast(`Volumen ${Math.round(v * 100)} %`, 1200);
      return { v };
    }
    return { fallback: 'key', key: delta >= 0 ? 'ArrowUp' : 'ArrowDown' };
  }

  function mute() {
    const p = npl();
    if (has(p, 'isMuted') && has(p, 'setMuted')) {
      const m = !p.isMuted(); p.setMuted(m);
      toast(m ? 'Silencio' : 'Sonido', 1200);
      return { muted: m };
    }
    return { fallback: 'key', key: 'm' };
  }

  function trackName(t, kind) {
    if (kind !== 'audio' && (t.isNoneTrack || t.isNone || t.isForcedNarrative || /^(off|none|desactivad)/i.test(t.displayName || ''))) return 'Desactivados';
    return t.displayName || t.languageDescription || t.language || t.bcp47 || t.trackId || '?';
  }

  function cycleTrack(kind) {
    const audio = kind === 'audio';
    const title = audio ? 'Audio' : 'Subtítulos';
    const p = npl();
    const getL = audio ? 'getAudioTrackList' : 'getTimedTextTrackList';
    const getC = audio ? 'getAudioTrack' : 'getTimedTextTrack';
    const setT = audio ? 'setAudioTrack' : 'setTimedTextTrack';
    if (!(has(p, getL) && has(p, setT))) { toast(`${title}: no disponible`); return { none: true }; }
    // Las pistas "forced narrative" (solo carteles) se omiten si hay una pista "none" aparte;
    // si no la hay, la forced es la opción "Desactivados" del menú de Netflix y se conserva.
    const all = (p[getL]() || []).filter(Boolean);
    const hasNone = all.some((t) => t.isNoneTrack || t.isNone);
    const list = all.filter((t) => !(t.isForcedNarrative && hasNone));
    if (!list.length) { toast(`${title}: sin opciones`); return { none: true }; }
    const cur = has(p, getC) ? p[getC]() : null;
    const curId = cur && (cur.trackId || cur.id);
    const idx = list.findIndex((t) => (t.trackId || t.id) === curId);
    const next = list[(idx + 1) % list.length];
    try { p[setT](next); } catch (e) { toast(`${title}: error`); return { error: String(e) }; }
    const name = trackName(next, kind);
    toast(`${title}: ${name}`, 2200);
    return { name, index: (idx + 1) % list.length, count: list.length };
  }

  function smart() {
    for (const pair of (C.skip || [])) {
      const el = $$(pair[0]).find((e) => usable(e, { inView: true }));
      if (el) return Object.assign(target(el), { label: pair[1] });
    }
    if (C.skip_text) {
      let re = null;
      try { re = new RegExp(C.skip_text, 'i'); } catch (e) { /* nada */ }
      if (re) {
        const el = $$('button, [role="button"], a').find((e) => usable(e, { inView: true }) && re.test(textOf(e)));
        if (el) return Object.assign(target(el), { label: textOf(el) });
      }
    }
    return { none: true, player: inPlayer() };
  }

  function nextEpisode() {
    const el = $$(C.next_episode).find((e) => e.getBoundingClientRect().width > 0);
    if (!el) return { none: true };
    try { el.click(); } catch (e) { /* nada */ }
    return { clicked: true, label: 'Siguiente episodio' };
  }

  function profile(n) {
    const links = profileLinks();
    if (!links.length) return { none: true, count: 0 };
    if (n < 1 || n > links.length) { toast(`No hay perfil ${n} (hay ${links.length})`); return { none: true, count: links.length }; }
    const el = links[n - 1];
    setCur(el, false);
    return Object.assign(target(el), { name: textOf(el), count: links.length });
  }

  function cwRowEl() {
    let row = $$(C.cw_row)[0] || null;
    if (!row && C.cw_text) {
      let re = null;
      try { re = new RegExp(C.cw_text, 'i'); } catch (e) { /* nada */ }
      const h = re && $$(C.row_header).find((x) => re.test(x.textContent || ''));
      if (h) row = closest(h, C.row) || h.parentElement;
    }
    return row;
  }

  function cw(scrollIfMissing) {
    const row = cwRowEl();
    if (!row) {
      if (scrollIfMissing) window.scrollBy({ top: vh() * 0.8 });
      return { none: true };
    }
    const links = $$('a[href*="/watch/"]', row);
    if (links.length) {
      const a = links.find((x) => usable(x)) || links[0];
      return { href: new URL(a.getAttribute('href'), location.href).href, title: textOf(a) };
    }
    const cards = $$(C.card, row);
    const card = cards.find((x) => usable(x)) || cards[0];
    if (card) { scrollToShow(card); return Object.assign(target(card), { opened: true, title: textOf(card) }); }
    return { none: true, row: true };
  }

  function playButton() {
    const { root } = current();
    const el = $$('[data-uia="play-button"], button[data-uia*="play"]', root).find((e) => usable(e, { inView: true }));
    if (!el) return { none: true };
    return Object.assign(target(el), { label: textOf(el) });
  }

  // Tecla PLAY fuera del reproductor: reproducir el título resaltado (o el de la ficha abierta).
  const PLAY_SEL = '[data-uia="play-button"], [data-uia="play-video-button"], button[data-uia*="play"]';
  function playSelected() {
    if (inPlayer()) return { none: true };
    const { modal } = current();
    if (modal) {
      const pb = playButton();
      return pb.none ? { none: true, modal: true } : pb;
    }
    const el = S.cur;
    if (!el || !el.isConnected) return { none: true };
    const play = matches(el, PLAY_SEL) ? el : closest(el, PLAY_SEL);
    if (play) return Object.assign(target(play), { label: textOf(play) });
    const card = matches(el, C.card) ? el : closest(el, C.card) || $$(C.card, el)[0];
    if (!card) return { none: true };
    const href = card.getAttribute('href') || '';
    const title = textOf(card);
    if (/\/watch\//.test(href)) return { href: new URL(href, location.href).href, title };
    scrollToShow(card);
    return Object.assign(target(card), { opened: true, title });
  }

  function navMode() {
    if (!inPlayer()) return { navMode: false };
    S.navMode = !S.navMode; S.navModePath = location.pathname;
    if (!S.navMode) setCur(null);
    toast(S.navMode ? 'Modo navegación (0 o Atrás para volver)' : 'Modo reproducción');
    return { navMode: S.navMode };
  }

  // ------------------------------------------------------------ diagnóstico
  function describe(el) {
    const r = R(el);
    const cls = typeof el.className === 'string' ? el.className : '';
    return {
      tag: el.tagName.toLowerCase(), cls: cls.slice(0, 60), uia: el.getAttribute('data-uia') || '',
      text: textOf(el).slice(0, 50), rect: [r.l, r.t, r.w, r.h].map(Math.round),
    };
  }

  function methods(o) {
    const out = new Set(); let x = o; let d = 0;
    while (x && x !== Object.prototype && d < 5) {
      for (const k of Object.getOwnPropertyNames(x)) { try { if (typeof o[k] === 'function') out.add(k); } catch (e) { /* nada */ } }
      x = Object.getPrototypeOf(x); d++;
    }
    return [...out].sort().slice(0, 150);
  }

  function dump() {
    const { root, modal } = current();
    const cands = candidates(root);
    const inView = cands.filter((c) => usable(c, { inView: true }));
    const uia = new Set();
    for (const e of $$('[data-uia]')) { const r = e.getBoundingClientRect(); if (r.width > 0 && r.height > 0) uia.add(e.getAttribute('data-uia')); }
    const p = npl();
    return {
      url: location.href, ctx: ctx().ctx, modal: !!modal, candidates: cands.length, inView: inView.length,
      current: S.cur ? describe(S.cur) : null, sample: inView.slice(0, 15).map(describe),
      dataUia: [...uia].slice(0, 200), playerApi: !!p, playerMethods: p ? methods(p) : [],
      profiles: profileLinks().map(textOf), cwRow: !!cwRowEl(), rows: $$(C.row).length,
      video: !!document.querySelector('video'),
      player: playerState(p),
      perf: perfInfo(),
    };
  }

  // Estado del reproductor (para el diagnóstico): tiempo, pausa, volumen y pistas.
  function playerState(p) {
    if (!p) return null;
    const call = (m) => { try { return has(p, m) ? p[m]() : undefined; } catch (e) { return undefined; } };
    const tt = call('getTimedTextTrack'); const at = call('getAudioTrack');
    const t = call('getCurrentTime'); const d = call('getDuration');
    return {
      time: typeof t === 'number' && isFinite(t) ? fmt(t / 1000) : null,
      duration: typeof d === 'number' && isFinite(d) ? fmt(d / 1000) : null,
      paused: call('isPaused') ?? null, volume: call('getVolume') ?? null, muted: call('isMuted') ?? null,
      audio: at ? trackName(at, 'audio') : null, subtitles: tt ? trackName(tt, 'text') : null,
    };
  }

  // ------------------------------------------------------------ API
  const API = {
    ctx, nav, afterPage, activate, back, restore, smart, cw, profile, playButton, playSelected, nextEpisode, navMode, toast, dump,
    busy, done, echo,
    profiles: () => ({ names: profileLinks().map(textOf) }),
    playPause: () => playPause(),
    play: () => playPause('play'),
    pause: () => playPause('pause'),
    seek, volume, mute,
    subtitles: () => cycleTrack('text'),
    audio: () => cycleTrack('audio'),
    hide: () => { setCur(null); return {}; },
    // Con los controles ocultos puede quedar visible un solo botón suelto (p. ej. "Saltar intro").
    controlsShown: () => ({ shown: !inPlayer() || anyShown(C.player_back) || candidates(current().root).length > 1 }),
    viewport: () => ({ w: vw(), h: vh() }),
  };

  function cmd(name, args) {
    const f = API[name];
    if (!f) return { error: `comando desconocido: ${name}` };
    try {
      const r = f.apply(null, args || []);
      return r === undefined ? {} : r;
    } catch (e) {
      return { error: String((e && e.stack) || e) };
    }
  }

  // ------------------------------------------------------------ rendimiento
  // Transiciones y animaciones a 1 ms (no "none"): así siguen disparándose los
  // eventos transitionend/animationend de los que depende el código de Netflix.
  // Además, fuera lo que más le cuesta a la GPU de la Pi al dibujar: desenfoques (backdrop-filter, filter),
  // sombras difusas (box-shadow, text-shadow), capas extra (will-change) y fondos fijos (se redibujan con
  // cada scroll). El marco de foco conserva un contorno sólido, sin difuminar.
  const PERF_CSS = `
*,*::before,*::after{transition-duration:1ms!important;transition-delay:0s!important;
  animation-duration:1ms!important;animation-delay:0s!important;animation-iteration-count:1!important;
  scroll-behavior:auto!important;backdrop-filter:none!important;-webkit-backdrop-filter:none!important;
  filter:none!important;box-shadow:none!important;text-shadow:none!important;will-change:auto!important;
  background-attachment:scroll!important}
#nfx-ring{box-shadow:0 0 0 3px #e50914,0 0 0 6px rgba(0,0,0,.55)!important}`;
  const PREVIEW_CSS = `html.nfx-browse video{visibility:hidden!important}`;

  function stopPreviews() {
    const browse = !inPlayer();
    if (document.documentElement) document.documentElement.classList.toggle('nfx-browse', browse);
    if (!browse) return;
    for (const v of document.querySelectorAll('video')) {
      v.autoplay = false;
      if (!v.paused) { try { v.pause(); } catch (e) { /* nada */ } }
    }
  }

  function addPerfStyle() {
    let css = '';
    if (C.reduce_effects) css += PERF_CSS;
    if (C.stop_previews) css += PREVIEW_CSS;
    if (!css || document.getElementById('nfx-perf')) return;
    const st = document.createElement('style');
    st.id = 'nfx-perf';
    st.textContent = css;
    (document.head || document.documentElement).appendChild(st);
  }

  // Las animaciones hechas con element.animate() (Web Animations) no las alcanza el CSS: se les fuerza
  // la misma duración de 1 ms. Las infinitas pasan a una sola vuelta (a 1 ms darían mil vueltas por segundo).
  function patchAnimate() {
    const proto = window.Element && Element.prototype;
    if (!proto || typeof proto.animate !== 'function' || proto.animate.__nfxOrig) return;
    const orig = proto.animate;
    const animate = function animate(keyframes, options) {
      let o = options;
      if (o && typeof o === 'object') {
        if (o.timeline) return orig.call(this, keyframes, options);  // atada al scroll, no al reloj
        o = Object.assign({}, o, { duration: 1, delay: 0, endDelay: 0 });
        if (!(Number(o.iterations) < Infinity)) o.iterations = 1;
      } else {
        o = 1;
      }
      return orig.call(this, keyframes, o);
    };
    animate.__nfxOrig = orig;
    proto.animate = animate;
    // Las que ya estaban corriendo (el script se inyectó con la página ya abierta).
    try {
      for (const a of document.getAnimations()) {
        if (a.constructor.name !== 'Animation' || a.playState !== 'running') continue;
        const it = a.effect && a.effect.getTiming ? a.effect.getTiming().iterations : 1;
        if (it < Infinity) a.finish();
      }
    } catch (e) { /* nada */ }
  }

  function unpatchAnimate() {
    const proto = window.Element && Element.prototype;
    if (proto && proto.animate && proto.animate.__nfxOrig) proto.animate = proto.animate.__nfxOrig;
  }

  // Para el diagnóstico: qué sigue animándose en la página pese a reduce_effects.
  function perfInfo() {
    let anims = [];
    try { anims = document.getAnimations().filter((a) => a.playState === 'running'); } catch (e) { /* nada */ }
    const who = (t) => {
      if (!t || t.nodeType !== 1) return '';
      const cls = typeof t.className === 'string' ? t.className.trim().split(/\s+/).slice(0, 2).join('.') : '';
      return t.tagName.toLowerCase() + (t.id ? '#' + t.id : '') + (cls ? '.' + cls : '') + (t.getAttribute('data-uia') ? `[${t.getAttribute('data-uia')}]` : '');
    };
    return {
      reduceEffects: !!C.reduce_effects,
      css: !!document.getElementById('nfx-perf'),
      reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
      animatePatched: !!(Element.prototype.animate && Element.prototype.animate.__nfxOrig),
      running: anims.length,
      sample: anims.slice(0, 15).map((a) => {
        const tm = a.effect && a.effect.getComputedTiming ? a.effect.getComputedTiming() : {};
        return {
          kind: a.constructor.name, name: a.animationName || a.transitionProperty || a.id || '',
          target: who(a.effect && a.effect.target) + (a.effect && a.effect.pseudoElement ? a.effect.pseudoElement : ''),
          ms: tm.duration, iterations: tm.iterations,
        };
      }),
    };
  }

  function initPerf() {
    if (C.reduce_effects) patchAnimate();
    // En una página nueva este script corre antes de que exista <html>: esperar a que aparezca.
    if (document.documentElement) addPerfStyle();
    else {
      const mo = new MutationObserver(() => { if (document.documentElement) { mo.disconnect(); addPerfStyle(); } });
      mo.observe(document, { childList: true });
    }
    if (C.stop_previews) {
      // Netflix es una SPA: se revisa periódicamente en vez de solo al cargar.
      S.onPlay = (e) => { if (!inPlayer() && e.target && e.target.tagName === 'VIDEO') e.target.pause(); };
      document.addEventListener('play', S.onPlay, true);
      S.perfTimer = setInterval(stopPreviews, 1000);
      stopPreviews();
    }
  }

  function destroy() {
    cancelAnimationFrame(S.raf);
    clearInterval(S.tickTimer);
    clearInterval(S.perfTimer);
    for (const [t, type, fn, opts] of S.listeners) t.removeEventListener(type, fn, opts);
    unpatchAnimate();
    if (S.onPlay) document.removeEventListener('play', S.onPlay, true);
    if (document.documentElement) document.documentElement.classList.remove('nfx-browse');
    clearInterval(S.busyTimer); clearTimeout(S.echoTimer);
    for (const id of ['nfx-ring', 'nfx-toast', 'nfx-busy', 'nfx-echo', 'nfx-style', 'nfx-perf']) { const e = document.getElementById(id); if (e) e.remove(); }
    if (S.cur && S.cur.classList) S.cur.classList.remove('nfx-focused');
  }

  on(document, 'visibilitychange', () => { if (!document.hidden && S.cur) showRing(); });
  on(document, 'fullscreenchange', () => { if (S.cur) showRing(); });
  // Mientras la página (o la ficha abierta) se desplaza, el marco sigue al elemento cuadro a cuadro.
  const onMove = () => { if (S.cur) follow(300); };
  on(document, 'scroll', onMove, { capture: true, passive: true });
  on(window, 'resize', onMove, { passive: true });

  window.__nfx = { v: V, cmd, destroy };
  initPerf();
  busyResume();
})();
