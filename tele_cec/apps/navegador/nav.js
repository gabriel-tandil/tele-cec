/*
 * tele-cec — script inyectado en cualquier página del Navegador (WebDriver BiDi en Firefox, DevTools en Chromium).
 * Expone window.__tcnav.cmd(nombre, args): moverse con las flechas entre los enlaces y botones (el más cercano en
 * esa dirección, con un marco), abrir, volver, escribir en el campo con el foco (los números del control, T9), el
 * cuadro «Ir a» para una dirección o una búsqueda, y menos animaciones.
 * __NAV_VERSION__ y __NAV_CONFIG__ los reemplaza el servicio al inyectar.
 */
(() => {
  'use strict';
  // El puntero del sistema no se ve nunca: en el modo mouse está la flecha dibujada, y sin él, el marco. Las páginas
  // ponen su propio cursor (mano en los enlaces, «I» en los campos), así que va con !important, y también en los
  // iframes (el «No soy un robot» de un captcha), donde el resto del script no corre.
  function hideCursor() {
    const add = () => {
      if (!document.documentElement || document.getElementById('tcn-nocursor')) return;
      const st = document.createElement('style');
      st.id = 'tcn-nocursor';
      st.textContent = '*,*::before,*::after{cursor:none!important}';
      (document.head || document.documentElement).appendChild(st);
    };
    add();
    document.addEventListener('DOMContentLoaded', add, { once: true });
    window.addEventListener('load', add, { once: true });
  }
  if (window.top !== window) { hideCursor(); return; }
  const V = "__NAV_VERSION__";
  if (window.__tcnav && window.__tcnav.v === V) return;
  if (window.__tcnav && typeof window.__tcnav.destroy === 'function') { try { window.__tcnav.destroy(); } catch (e) { /* nada */ } }
  hideCursor();
  const C = __NAV_CONFIG__;

  const S = { cur: null, opener: null, lastRect: null, mouse: null, armed: null, raf: 0, ringKey: '', toastTimer: 0, hintTimer: 0, tickTimer: 0, followUntil: 0, listeners: [] };
  function on(target, type, fn, opts) { target.addEventListener(type, fn, opts); S.listeners.push([target, type, fn, opts]); }

  // ------------------------------------------------------------ utilidades
  const $$ = (sel, root) => { if (!sel) return []; try { return Array.from((root || document).querySelectorAll(sel)); } catch (e) { return []; } };
  const matches = (el, sel) => { if (!sel || !el || !el.matches) return false; try { return el.matches(sel); } catch (e) { return false; } };
  const closest = (el, sel) => { if (!sel || !el || !el.closest) return null; try { return el.closest(sel); } catch (e) { return null; } };
  const vw = () => window.innerWidth;
  const vh = () => window.innerHeight;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const textOf = (el) => ((el && (el.getAttribute('aria-label') || el.innerText || el.textContent || el.getAttribute('placeholder') || el.value || el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('href'))) || '')
    .replace(/\s+/g, ' ').trim().slice(0, 80);
  const TEXT_SEL = 'input:not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="submit"]):not([type="range"]):not([type="file"]):not([type="color"]), textarea, [contenteditable=""], [contenteditable="true"], [contenteditable="plaintext-only"]';
  const GO_ID = 'tcn-go';
  const behavior = () => (C.reduce_effects ? 'auto' : 'smooth');

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

  // ¿Se puede enfocar? (visible, mayormente dentro de la pantalla en horizontal, cerca en vertical)
  function usable(el, opts) {
    opts = opts || {};
    if (!el || !el.isConnected) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    if (opts.near !== false && (r.bottom < -vh() * 1.5 || r.top > vh() * 2.5)) return false;
    const visW = Math.min(r.right, vw()) - Math.max(r.left, 0);
    if (visW < Math.min(r.width, vw()) * 0.6 && !hScroller(el)) return false;
    if (opts.inView) {
      const visH = Math.min(r.bottom, vh()) - Math.max(r.top, 0);
      if (visH < Math.min(r.height, vh()) * 0.5) return false;
    }
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    if (closest(el, '[aria-hidden="true"], [inert]')) return false;
    return cssVisible(el);
  }

  // Una franja con desplazamiento lateral que contiene al elemento y se ve (las pestañas de arriba que no entran, las
  // columnas de la derecha de una tabla ancha): lo que está corrido afuera también se puede elegir, y al elegirlo se
  // corre la franja (scrollIntoView en setCur).
  function hScroller(el) {
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      if (p.scrollWidth <= p.clientWidth + 4) continue;
      if (!/(auto|scroll|overlay)/.test(getComputedStyle(p).overflowX)) continue;
      const pr = p.getBoundingClientRect();
      if (Math.min(pr.right, vw()) - Math.max(pr.left, 0) < 40) return null;
      return p;
    }
    return null;
  }

  // ¿Lo tapa el borde de algún contenedor con desplazamiento (una tabla con su propia barra, una franja)?
  function clipped(el) {
    const r = el.getBoundingClientRect();
    for (let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      const cs = getComputedStyle(p);
      if (!/(auto|scroll|overlay)/.test(cs.overflowX + cs.overflowY)) continue;
      const pr = p.getBoundingClientRect();
      if (r.left < pr.left - 2 || r.right > pr.right + 2 || r.top < pr.top - 2 || r.bottom > pr.bottom + 2) return true;
    }
    return false;
  }

  // Un cuadro encima de la página (confirmar, comentarios…): las flechas se quedan adentro y Atrás lo cierra.
  const MODAL_SEL = 'dialog[open], [aria-modal="true"], [role="dialog"], [role="alertdialog"], .modal-overlay, .modal-backdrop';
  // Un [role=dialog] sin aria-modal cuenta solo si es grande (un chat chico en una esquina no atrapa las flechas), y un
  // <dialog> solo si es modal (showModal).
  function isModal(m) {
    if (closest(m, '#tcn-go') || !cssVisible(m)) return false;
    const r = m.getBoundingClientRect();
    if (r.width < 40 || r.height < 20) return false;
    if (m.tagName === 'DIALOG') { try { return m.matches(':modal'); } catch (e) { return true; } }
    if (m.getAttribute('aria-modal') === 'true' || matches(m, '.modal-overlay, .modal-backdrop') || overlayOf(m)) return true;
    const area = Math.max(0, Math.min(r.right, vw()) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, vh()) - Math.max(r.top, 0));
    return area >= vw() * vh() * 0.2;
  }
  // La capa fija que tapa toda la pantalla alrededor de un cuadro (el fondo oscuro): Atrás hace clic ahí si el cuadro
  // no tiene cómo cerrarse.
  function overlayOf(m) {
    for (let p = m.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      if (getComputedStyle(p).position !== 'fixed') continue;
      const r = p.getBoundingClientRect();
      if (r.width >= vw() * 0.9 && r.height >= vh() * 0.9) return p;
    }
    return null;
  }
  function modal() {
    const list = $$(MODAL_SEL).filter(isModal);
    for (let i = list.length - 1; i >= 0; i--) {
      if ($$(C.focusable, list[i]).some((e) => usable(e, { near: false }))) return overlayOf(list[i]) || list[i];
    }
    return null;
  }
  const scope = () => modal() || document;

  // Filas que se abren con un clic (el detalle de una descarga): se elige su primera celda con texto, la que no tiene
  // adentro nada que se pueda elegir (la casilla de la fila sigue siendo aparte).
  function rowCells(root) {
    const out = [];
    for (const tr of $$(C.rows, root)) {
      for (const td of tr.children) {
        if (!/^T[DH]$/.test(td.tagName) || $$(C.focusable, td).length || !(td.innerText || '').trim()) continue;
        if (usable(td)) out.push(td);
        break;
      }
    }
    return out;
  }

  // Candidatos a foco: solo las «hojas» (si un enlace tiene adentro un botón, se usa el de adentro).
  function candidates(root, opts) {
    root = root || document;
    const all = $$(C.focusable, root).filter((el) => !matches(el, C.ignore) && usable(el, opts)).concat(rowCells(root));
    const set = new Set(all);
    const parents = new Set();
    for (const el of all) {
      let p = el.parentElement;
      while (p && p !== root) { if (set.has(p)) parents.add(p); p = p.parentElement; }
    }
    return all.filter((el) => !parents.has(el));
  }

  // ------------------------------------------------------------ resaltado y avisos
  function ensureStyle() {
    if (document.getElementById('tcn-style')) return;
    const st = document.createElement('style');
    st.id = 'tcn-style';
    st.textContent = `
#tcn-ring{position:fixed;left:0;top:0;width:0;height:0;pointer-events:none;z-index:2147483647;box-sizing:border-box;
  border:4px solid #fff;border-radius:8px;box-shadow:0 0 0 3px #2f7cf6,0 0 0 6px rgba(0,0,0,.55);opacity:0}
#tcn-ring.on{opacity:1}
#tcn-toast{position:fixed;left:50%;bottom:8vh;transform:translate(-50%,0);max-width:84vw;box-sizing:border-box;
  padding:.5em 1.1em;border-radius:10px;border-left:6px solid #2f7cf6;background:rgba(20,20,24,.92);color:#fff;
  font:600 clamp(18px,2vw,40px)/1.3 system-ui,Helvetica,Arial,sans-serif;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;z-index:2147483647;pointer-events:none;opacity:0}
#tcn-toast.on{opacity:1}
#tcn-hint{position:fixed;left:50%;top:3vh;transform:translate(-50%,0);box-sizing:border-box;padding:.35em .9em;
  border-radius:12px;background:rgba(20,20,24,.92);color:#bbb;font:700 clamp(22px,2.6vw,52px)/1.2 system-ui,Helvetica,Arial,sans-serif;
  letter-spacing:.25em;z-index:2147483647;pointer-events:none;opacity:0;white-space:nowrap}
#tcn-hint.on{opacity:1}
#tcn-hint b{color:#fff;background:#2f7cf6;border-radius:6px;padding:0 .15em}
#tcn-go{position:fixed;left:50%;top:6vh;transform:translate(-50%,0);width:min(90vw,1400px);box-sizing:border-box;display:flex;
  align-items:center;gap:.6em;padding:.5em .9em;border-radius:14px;background:rgba(20,20,24,.96);color:#fff;
  font:600 clamp(18px,2vw,40px)/1.3 system-ui,Helvetica,Arial,sans-serif;z-index:2147483646;box-shadow:0 8px 30px rgba(0,0,0,.6);
  border:3px solid #2f7cf6}
#tcn-go span{flex:none;color:#9cc0ff}
#tcn-go input{flex:1;min-width:0;font:inherit;font-weight:500;color:#fff;background:#101014;border:2px solid #444;border-radius:8px;
  padding:.25em .5em;outline:none}
#tcn-go input:focus{border-color:#fff}
#tcn-go small{flex:none;color:#888;font-size:.55em;font-weight:500}
#tcn-cursor{position:fixed;left:0;top:0;width:44px;height:44px;margin:-3px 0 0 -5px;pointer-events:none;z-index:2147483647;
  filter:drop-shadow(0 2px 4px rgba(0,0,0,.6))}
#tcn-cursor svg{width:100%;height:100%;display:block}
#tcn-ring,#tcn-toast,#tcn-hint,#tcn-cursor{display:block!important;visibility:visible!important}
#tcn-go{visibility:visible!important}
#tcn-cursor .hand{display:none}
#tcn-cursor.link .arrow{display:none}
#tcn-cursor.link .hand{display:block}`;
    (document.head || document.documentElement).appendChild(st);
  }

  // Lo de tele-cec (marco, flecha, avisos) cuelga de <html>, no de <body>: hay páginas que al abrir algo ocultan todo
  // lo demás del body (el visor de fotos de Wikipedia: «body.mw-mmv-lightbox-open > * {display: none}»).
  function overlay(id) {
    let e = document.getElementById(id);
    if (!e) { e = document.createElement('div'); e.id = id; }
    const host = document.fullscreenElement || document.documentElement;
    if (e.parentNode !== host) host.appendChild(e);
    return e;
  }

  function positionRing() {
    const ring = overlay('tcn-ring');
    if (S.cur && !S.cur.isConnected) {
      const rec = recover();
      if (rec.el && rec.near) S.cur = rec.el; else S.cur = null;
    }
    if (!S.cur) { ring.classList.remove('on'); S.ringKey = ''; return false; }
    const r = S.cur.getBoundingClientRect();
    const pad = 5;
    const key = [r.left, r.top, r.width, r.height].map(Math.round).join(',');
    if (key !== S.ringKey) {
      S.ringKey = key;
      ring.style.transform = `translate(${Math.round(r.left - pad)}px,${Math.round(r.top - pad)}px)`;
      ring.style.width = `${Math.round(r.width + 2 * pad)}px`;
      ring.style.height = `${Math.round(r.height + 2 * pad)}px`;
    }
    if (!ring.classList.contains('on')) ring.classList.add('on');
    return true;
  }

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
    if (!usable(S.cur, { near: false })) { const rec = recover(); setCur(rec.el && rec.near ? rec.el : null, false); return; }
    positionRing();
  }
  function showRing() {
    ensureStyle(); positionRing(); follow(400);
    if (!S.tickTimer) S.tickTimer = setInterval(ringTick, 250);
  }
  function hideRing() {
    const ring = document.getElementById('tcn-ring');
    if (ring) ring.classList.remove('on');
    S.ringKey = '';
    cancelAnimationFrame(S.raf); S.raf = 0;
    clearInterval(S.tickTimer); S.tickTimer = 0;
  }

  function toast(text, ms) {
    ensureStyle();
    const t = overlay('tcn-toast');
    t.textContent = String(text);
    t.classList.add('on');
    clearTimeout(S.toastTimer);
    S.toastTimer = setTimeout(() => t.classList.remove('on'), ms || 1800);
    return { ok: true };
  }

  // Las letras de la tecla apretada, con la elegida marcada («a b c»), arriba de la pantalla.
  function hint(letters, idx, ms) {
    ensureStyle();
    const h = overlay('tcn-hint');
    h.replaceChildren();
    if (letters) {
      Array.from(letters).forEach((ch, i) => {
        const e = document.createElement(i === idx ? 'b' : 'span');
        e.textContent = ch === ' ' ? '␣' : ch;
        h.appendChild(e);
      });
    } else {
      h.textContent = '';
    }
    h.classList.toggle('on', !!letters);
    clearTimeout(S.hintTimer);
    if (letters) S.hintTimer = setTimeout(() => h.classList.remove('on'), ms || 1500);
    return { ok: true };
  }

  // ------------------------------------------------------------ foco
  function scrollToShow(el) {
    const r = el.getBoundingClientRect();
    const margin = Math.min(120, vh() * 0.12);
    if (r.top >= margin && r.bottom <= vh() - margin) return;
    if (r.height > vh() - 2 * margin && r.top >= 0 && r.top < vh()) return;
    const delta = Math.round((r.top + r.height / 2) - vh() / 2);
    let p = el.parentElement;
    while (p && p !== document.body && p !== document.documentElement) {
      const oy = getComputedStyle(p).overflowY;
      if (/(auto|scroll|overlay)/.test(oy) && p.scrollHeight > p.clientHeight + 4) { p.scrollBy({ top: delta, behavior: behavior() }); return; }
      p = p.parentElement;
    }
    window.scrollBy({ top: delta, behavior: behavior() });
  }

  function setCur(el, scroll) {
    if (S.cur && S.cur !== el && S.cur.classList) S.cur.classList.remove('tcn-focused');
    S.cur = el || null;
    if (!el) { hideRing(); return; }
    el.classList.add('tcn-focused');
    if (scroll !== false && clipped(el)) { try { el.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'auto' }); } catch (e) { /* nada */ } }
    if (scroll !== false) scrollToShow(el);
    S.lastRect = R(el);
    showRing();
  }

  function initial(cands) {
    const list = (cands || candidates(scope())).filter((e) => usable(e, { inView: true }));
    list.sort((a, b) => { const ra = R(a); const rb = R(b); return (Math.round(ra.t / 40) - Math.round(rb.t / 40)) || (ra.l - rb.l); });
    // Mejor empezar por el contenido que por el menú de arriba: el primero que no esté pegado al borde superior.
    return list.find((e) => R(e).t > vh() * 0.08) || list[0] || null;
  }

  function recover() {
    const cands = candidates(scope());
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
    return { el: initial(cands), near: false };
  }

  // El vecino en esa dirección. Tiene que estar de ese lado (su centro pasando el borde del actual: así lo que
  // está en la misma fila, apenas más abajo, no cuenta como «abajo»). Con ◀ ▶ vale lo de la misma fila; otra fila
  // solo si está del todo a ese lado y cerca (si no, desde el «+» de un buscador ◀ saltaba al pie de la página).
  // Con ▲ ▼ se salta el enlace que va al mismo lugar que el actual (título y dirección de un mismo resultado).
  function pick(from, cands, dir) {
    const horizontal = dir === 'left' || dir === 'right';
    const tol = 4;
    const href = hrefOf(S.cur);
    let best = null; let bestScore = Infinity; let same = null; let sameScore = Infinity;
    for (const c of cands) {
      if (c === S.cur) continue;
      const r = R(c);
      let primary;
      if (dir === 'right') { if (r.cx < from.r - tol || r.r <= from.r) continue; primary = Math.max(0, r.l - from.r); }
      else if (dir === 'left') { if (r.cx > from.l + tol || r.l >= from.l) continue; primary = Math.max(0, from.l - r.r); }
      else if (dir === 'down') { if (r.cy < from.b - tol || r.b <= from.b) continue; primary = Math.max(0, r.t - from.b); }
      else { if (r.cy > from.t + tol || r.t >= from.t) continue; primary = Math.max(0, from.t - r.b); }
      let gap; let off;
      if (horizontal) {
        gap = Math.max(0, Math.max(r.t, from.t) - Math.min(r.b, from.b));
        off = Math.abs(r.cy - from.cy);
        if (gap > 0) {
          const beyond = dir === 'right' ? r.l >= from.r - tol : r.r <= from.l + tol;
          const near = gap <= Math.min(vh() * 0.25, Math.max(48, 1.2 * Math.max(from.h, r.h)));
          if (!beyond || !near) continue;
        }
      } else {
        gap = Math.max(0, Math.max(r.l, from.l) - Math.min(r.r, from.r));
        off = Math.min(Math.abs(r.cx - from.cx), Math.abs(r.l - from.l));  // las listas de enlaces van alineadas a la izquierda
      }
      const score = primary + gap * (horizontal ? 5 : 2) + off * (horizontal ? 0.5 : 0.25);
      if (!horizontal && href && hrefOf(c) === href) { if (score < sameScore) { sameScore = score; same = c; } continue; }
      if (score < bestScore) { bestScore = score; best = c; }
    }
    return best || same;
  }

  function hrefOf(el) {
    const a = el && closest(el, 'a[href]');
    return a ? a.href.replace(/#.*$/, '') : '';
  }

  function current() {
    if (S.cur && (!S.cur.isConnected || !usable(S.cur, { near: false }))) setCur(null);
  }

  function nav(dir) {
    current();
    const a = document.activeElement;
    // Un buscador con sus sugerencias abiertas (Google, YouTube, Wikipedia…): ▲ ▼ las recorren con flechas de
    // verdad (la página pone la elegida en el campo; OK la busca). Sin sugerencias, salen del campo.
    if (a && (dir === 'up' || dir === 'down') && matches(a, TEXT_SEL) && !isGoBox(a) && a.getAttribute('aria-expanded') === 'true') {
      return { key: dir === 'down' ? 'ArrowDown' : 'ArrowUp' };
    }
    if (a && a !== document.body && matches(a, TEXT_SEL) && !isGoBox(a)) a.blur();  // salir del campo
    if (!S.cur) {
      const rec = recover();
      if (!rec.el || !rec.near) { setCur(rec.el); return { focused: !!rec.el, label: textOf(rec.el) }; }
      setCur(rec.el, false);
    }
    const sc = scope();
    if (sc !== document && !sc.contains(S.cur)) {  // se abrió un cuadro encima: empezar adentro
      S.opener = S.cur; S.lastRect = null; setCur(recover().el); return { focused: !!S.cur, label: textOf(S.cur) };
    }
    if (clipped(S.cur)) { try { S.cur.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'auto' }); } catch (e) { /* nada */ } }
    const from = R(S.cur);
    let best = pick(from, candidates(sc), dir);
    if (best) { setCur(best); return { moved: true, label: textOf(best) }; }
    // Nada en esa dirección a la vista: se corre la página y se vuelve a buscar.
    const vertical = dir === 'up' || dir === 'down';
    const before = window.scrollY;
    if (vertical) window.scrollBy({ top: (dir === 'down' ? 1 : -1) * vh() * 0.6, behavior: 'auto' });
    if (vertical && window.scrollY !== before) {
      const moved = window.scrollY - before;
      const from2 = { l: from.l, r: from.r, cx: from.cx, t: from.t - moved, b: from.b - moved, cy: from.cy - moved };
      best = pick(from2, candidates(sc), dir);
      if (best) { setCur(best); return { moved: true, scrolled: true, label: textOf(best) }; }
      S.lastRect = null;
      const rec = recover();
      if (rec.el) setCur(rec.el, false);
      return { scrolled: true, label: textOf(S.cur) };
    }
    return { edge: true };
  }

  function scroll(dir) {
    const delta = (dir === 'up' ? -1 : 1) * vh() * 0.85;
    window.scrollBy({ top: delta, behavior: behavior() });
    if (S.cur && !S.mouse) { S.lastRect = null; const rec = recover(); setCur(rec.el, false); }
    if (S.mouse) drawCursor();
    return { scrolled: true };
  }

  // Punto para un clic «real» (muchos sitios ignoran los clics por script): uno que caiga de verdad sobre el
  // elemento. El centro del recuadro no siempre sirve: un enlace en dos renglones (el centro cae entre ellos), un
  // título con un ícono o un botón encima. Se prueban el centro, cada renglón y lo de adentro (el título, la imagen).
  // Solo si ninguno cae sobre él, clic por script. El servicio después pregunta si el clic llegó (clicked).
  function target(el) {
    let r = el.getBoundingClientRect();
    if (r.bottom < 0 || r.top > vh()) { scrollToShow(el); r = el.getBoundingClientRect(); }
    const rects = [r, ...Array.from(el.getClientRects())];
    for (const d of $$('h1, h2, h3, h4, img, svg, span, div', el).slice(0, 16)) rects.push(d.getBoundingClientRect());
    let loose = null;
    for (const rr of rects) {
      if (rr.width < 2 || rr.height < 2 || rr.bottom < 1 || rr.top > vh() - 1 || rr.right < 1 || rr.left > vw() - 1) continue;
      for (const fx of [0.5, 0.2]) {
        const x = Math.round(clamp(rr.left + rr.width * fx, 1, vw() - 2));
        const y = Math.round(clamp(rr.top + rr.height / 2, 1, vh() - 2));
        const hit = document.elementFromPoint(x, y);
        if (hit && (hit === el || el.contains(hit))) { arm(el); return { x, y }; }
        if (!loose && hit && hit.contains(el)) loose = { x, y };  // un contenedor transparente encima: puede andar
      }
    }
    if (loose) { arm(el); return loose; }
    S.armed = null;
    try { el.click(); } catch (e) { /* nada */ }
    return { clicked: true };
  }

  // ¿Le llegó el clic de verdad? (si cayó en otra cosa, el elemento recibe un clic por script)
  function arm(el) { S.armed = { el, hit: false }; }
  function clicked() {
    const a = S.armed;
    S.armed = null;
    if (!a || a.hit || !a.el.isConnected) return { hit: !!(a && a.hit) };
    try { a.el.click(); } catch (e) { /* nada */ }
    return { hit: false, fallback: true };
  }

  const isNumeric = (el) => !!el && (el.type === 'number' || el.type === 'tel' || /^(numeric|decimal|tel)$/.test(el.inputMode || ''));

  function activate() {
    current();
    if (!S.cur) { const rec = recover(); setCur(rec.el); return { focused: !!rec.el, label: textOf(rec.el) }; }
    const el = S.cur;
    if (matches(el, TEXT_SEL)) {
      try { el.focus(); } catch (e) { /* nada */ }
      const len = (el.value || '').length;
      try { if (typeof el.setSelectionRange === 'function') el.setSelectionRange(len, len); } catch (e) { /* nada */ }
      return { text: true, numeric: isNumeric(el), label: textOf(el) || el.placeholder || '' };
    }
    if (matches(el, 'select')) {  // la lista desplegable no se puede abrir por script: pasar a la opción siguiente
      el.selectedIndex = (el.selectedIndex + 1) % Math.max(1, el.options.length);
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return { changed: true, label: el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : '' };
    }
    return Object.assign(target(el), { label: textOf(el) });
  }

  function back() {
    const a = document.activeElement;
    if (a && isGoBox(a)) { closeGo(); return { done: 'go_closed' }; }
    if (a && a !== document.body && matches(a, TEXT_SEL)) { a.blur(); return { done: 'blur' }; }
    if (document.fullscreenElement) { try { document.exitFullscreen(); } catch (e) { /* nada */ } return { done: 'fullscreen' }; }
    const m = modal();
    if (m) { closeModal(m); return { done: 'modal' }; }
    // ¿Hay a dónde volver? navigation.canGoBack lo sabe (history.length cuenta también las de adelante).
    const nav = window.navigation;
    const can = nav && typeof nav.canGoBack === 'boolean' ? nav.canGoBack : history.length > 1;
    if (can) { history.back(); return { done: 'history' }; }
    return { done: 'none' };
  }

  // Primero Escape (como en una compu); si el cuadro sigue, su botón de cerrar o cancelar; si no tiene, un clic en el
  // fondo oscuro (muchos cierran así).
  const CLOSE_RE = /^(×|✕|✖|x)$|close|cancel|dismiss|cerrar|cancelar|^no$/i;
  function closeModal(m) {
    const t = document.activeElement && document.activeElement !== document.body ? document.activeElement : document;
    for (const type of ['keydown', 'keyup']) {
      try { t.dispatchEvent(new KeyboardEvent(type, { key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true, cancelable: true })); } catch (e) { /* nada */ }
    }
    setTimeout(() => {
      if (m.isConnected && cssVisible(m)) {
        const btn = $$('button, [role="button"], a[href]', m).find((b) => usable(b, { near: false }) &&
          CLOSE_RE.test((b.getAttribute('aria-label') || b.getAttribute('title') || b.innerText || '').trim()));
        if (btn) btn.click();
        else if (m.getBoundingClientRect().width >= vw() * 0.9) m.click();
      }
      setTimeout(afterModal, 100);
    }, 150);
  }

  // Cerrado el cuadro, el marco vuelve a lo que lo abrió (o se queda donde estaba, si nunca entró al cuadro).
  function afterModal() {
    const back = [S.cur, S.opener].find((e) => e && e.isConnected && !closest(e, MODAL_SEL) && usable(e, { near: false }));
    S.opener = null;
    setCur(back || null);
  }

  // ------------------------------------------------------------ escribir
  function editable() {
    const a = document.activeElement;
    return a && a !== document.body && matches(a, TEXT_SEL) ? a : null;
  }

  function insert(el, ch, replace) {
    try { el.focus(); } catch (e) { /* nada */ }
    if (replace) { try { document.execCommand('delete'); } catch (e) { /* nada */ } }
    let ok = false;
    try { ok = document.execCommand('insertText', false, ch); } catch (e) { ok = false; }
    if (!ok && typeof el.setRangeText === 'function') {
      try {
        const s = el.selectionStart == null ? (el.value || '').length : el.selectionStart;
        const e = el.selectionEnd == null ? s : el.selectionEnd;
        el.setRangeText(ch, s, e, 'end');
        el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: ch }));
        ok = true;
      } catch (e) { ok = false; }
    }
    if (!ok && 'value' in el) {
      el.value = (replace ? String(el.value || '').slice(0, -1) : String(el.value || '')) + ch;
      el.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }

  function type(ch, replace, letters, idx) {
    const el = editable();
    if (!el) return { error: 'no hay un campo de texto con el foco' };
    insert(el, String(ch), !!replace);
    if (letters) hint(letters, idx);
    return { ok: true, value: isGoBox(el) ? el.value : undefined };
  }

  function clear() {
    const el = editable();
    if (!el) return { error: 'no hay un campo de texto con el foco' };
    if (el.isContentEditable) { try { document.execCommand('selectAll'); document.execCommand('delete'); } catch (e) { /* nada */ } }
    else { el.value = ''; el.dispatchEvent(new Event('input', { bubbles: true })); }
    return { ok: true };
  }

  // El cuadro «Ir a»: una dirección o qué buscar. Vive en la página: con la navegación desaparece.
  function isGoBox(el) { return !!(el && el.id === GO_ID + '-input'); }
  function go(prefill) {
    ensureStyle();
    const box = overlay(GO_ID);
    let input = document.getElementById(GO_ID + '-input');
    if (!input) {
      box.replaceChildren();
      const label = document.createElement('span'); label.textContent = 'Ir a';
      input = document.createElement('input'); input.id = GO_ID + '-input'; input.type = 'text';
      input.autocomplete = 'off'; input.spellcheck = false; input.placeholder = 'una dirección o qué buscar';
      const help = document.createElement('small'); help.textContent = C.go_hint || '';
      box.append(label, input, help);
    }
    if (prefill != null) input.value = String(prefill);
    setCur(null);
    try { input.focus(); const n = input.value.length; input.setSelectionRange(n, n); } catch (e) { /* nada */ }
    return { ok: true };
  }
  function closeGo() {
    const box = document.getElementById(GO_ID);
    if (box) box.remove();
    hint('');
  }

  function submit() {
    const el = editable();
    if (!el) return { enter: true };
    if (isGoBox(el)) { const v = el.value.trim(); closeGo(); return { go: v }; }
    const form = el.form;
    if (form && typeof form.requestSubmit === 'function' && !el.matches('textarea')) {
      try { form.requestSubmit(); return { submitted: true }; } catch (e) { /* nada */ }
    }
    return { enter: true };  // el servicio manda Enter de verdad
  }

  // m: {x, y} (o {}) si el modo mouse está prendido: lo recuerda el servicio, porque con cada página nueva este
  // script arranca de cero.
  function ctx(m) {
    if (m && typeof m === 'object') { if (!S.mouse) mouseShow(m.x, m.y); }
    else if (S.mouse) mouseHide(false);
    const el = editable();
    if (el && !S.mouse && el !== S.cur && !isGoBox(el) && usable(el, { near: false })) setCur(el, false);  // el campo que enfocó la página
    return { ctx: el ? 'text' : 'page', gobox: !!(el && isGoBox(el)), url: location.href, title: document.title };
  }

  function playPause(what) {
    const vids = Array.from(document.querySelectorAll('video, audio')).filter((v) => v.readyState > 0 || !v.paused);
    const v = vids.find((x) => !x.paused) || vids[0];
    if (!v) return { none: true };
    const play = what === 'play' || (what !== 'pause' && v.paused);
    try { if (play) v.play(); else v.pause(); } catch (e) { /* nada */ }
    return { playing: play };
  }

  function dump() {
    return { url: location.href, title: document.title, focused: textOf(S.cur), candidates: candidates(scope()).length, ctx: editable() ? 'text' : 'page', mouse: S.mouse };
  }

  // ------------------------------------------------------------ modo mouse
  // Una flecha dibujada que se mueve con las flechas del control; el clic lo hace el servicio, de verdad, en ese
  // punto (sirve también dentro de los iframes, adonde el marco no llega: un «No soy un robot», un video embebido).
  const CURSOR_SVG = '<svg class="arrow" viewBox="0 0 24 24"><path d="M4 2l15 11.5-6.6.9 3.9 7.4-3 1.6-3.9-7.5L4 20.6z" ' +
    'fill="#fff" stroke="#000" stroke-width="1.6" stroke-linejoin="round"/></svg>' +
    '<svg class="hand" viewBox="0 0 24 24"><path d="M9 11V4.5a1.5 1.5 0 013 0V10l.1-1a1.5 1.5 0 013 .1v1.3a1.5 1.5 0 013 .2v1.2a1.5 1.5 0 013 .3V16c0 3.6-2.6 6-6 6h-1.6c-2 0-3.4-.8-4.6-2.3L4.6 15a1.6 1.6 0 012.4-2.1L9 15z" ' +
    'fill="#fff" stroke="#000" stroke-width="1.5" stroke-linejoin="round"/></svg>';

  function clickableAt(x, y) {
    const hit = document.elementFromPoint(x, y);
    if (!hit) return null;
    if (hit.tagName === 'IFRAME') return hit;  // adentro no se ve: puede ser un botón
    return closest(hit, C.focusable) || (getComputedStyle(hit).cursor === 'pointer' ? hit : null);
  }

  function drawCursor() {
    if (!S.mouse) return;
    ensureStyle();
    const c = overlay('tcn-cursor');
    if (!c.firstChild) c.innerHTML = CURSOR_SVG;
    c.style.transform = `translate(${Math.round(S.mouse.x)}px,${Math.round(S.mouse.y)}px)`;
    const el = clickableAt(S.mouse.x, S.mouse.y);
    c.classList.toggle('link', !!el);
    S.mouse.label = el ? textOf(el) : '';
  }

  function mouseShow(x, y) {
    if (x == null || y == null || !isFinite(+x) || !isFinite(+y)) {
      // Donde estaba el marco (o en el medio de la pantalla).
      const el = S.cur && S.cur.isConnected ? S.cur : null;
      const r = el ? R(el) : null;
      x = r ? r.cx : vw() / 2; y = r ? r.cy : vh() / 2;
    }
    S.mouse = { x: clamp(+x, 0, maxX()), y: clamp(+y, 0, maxY()), label: '' };
    if (S.cur) { S.cur.classList.remove('tcn-focused'); S.cur = null; }
    hideRing();
    drawCursor();
    return { on: true, x: S.mouse.x, y: S.mouse.y, label: S.mouse.label };
  }

  function mouseHide(focus) {
    const m = S.mouse;
    S.mouse = null;
    const c = document.getElementById('tcn-cursor');
    if (c) c.remove();
    if (focus !== false && m) {  // el marco, en lo más cercano a donde quedó la flecha
      S.lastRect = { l: m.x, r: m.x, t: m.y, b: m.y, w: 0, h: 0, cx: m.x, cy: m.y };
      const rec = recover();
      if (rec.el) setCur(rec.el, false);
    }
    return { on: false };
  }

  // Hasta dónde llega la punta: la flecha se dibuja hacia abajo y a la derecha, que se siga viendo en el borde.
  const maxX = () => Math.max(0, vw() - 24);
  const maxY = () => Math.max(0, vh() - 30);

  // El contenedor que se puede correr debajo de la flecha (o la página).
  function scrollerAt(x, y, vertical) {
    let p = document.elementFromPoint(x, y);
    while (p && p !== document.body && p !== document.documentElement) {
      const cs = getComputedStyle(p);
      const ov = vertical ? cs.overflowY : cs.overflowX;
      if (/(auto|scroll|overlay)/.test(ov) && (vertical ? p.scrollHeight > p.clientHeight + 4 : p.scrollWidth > p.clientWidth + 4)) return p;
      p = p.parentElement;
    }
    return null;
  }

  // Mueve la flecha; si se pasa del borde, corre la página (o la lista que está debajo) en vez de salirse.
  function mouseMove(dx, dy) {
    if (!S.mouse) mouseShow();  // (el campo con el foco lo sigue teniendo, como con un mouse: hasta el próximo clic)
    dx = +dx || 0; dy = +dy || 0;
    const m = S.mouse;
    const nx = m.x + dx; const ny = m.y + dy;
    const cx = clamp(nx, 0, maxX()); const cy = clamp(ny, 0, maxY());
    let scrolled = false;
    const over = { x: nx - cx, y: ny - cy };
    for (const vertical of [true, false]) {
      const amount = vertical ? over.y : over.x;
      if (!amount) continue;
      const by = Math.sign(amount) * Math.max(Math.abs(amount), vertical ? vh() * 0.15 : vw() * 0.15);
      const sc = scrollerAt(cx, cy, vertical);
      const before = sc ? (vertical ? sc.scrollTop : sc.scrollLeft) : (vertical ? window.scrollY : window.scrollX);
      if (sc) sc.scrollBy(vertical ? { top: by } : { left: by });
      else window.scrollBy(vertical ? { top: by } : { left: by });
      const after = sc ? (vertical ? sc.scrollTop : sc.scrollLeft) : (vertical ? window.scrollY : window.scrollX);
      if (after !== before) scrolled = true;
    }
    m.x = cx; m.y = cy;
    drawCursor();
    return { x: m.x, y: m.y, scrolled, edge: !scrolled && (over.x !== 0 || over.y !== 0), label: m.label };
  }

  function mouse(what, a, b) {
    if (what === 'show') return mouseShow(a, b);
    if (what === 'hide') return mouseHide(true);
    if (what === 'move') return mouseMove(a, b);
    if (what === 'where') {  // para el clic: el punto, y si ahí hay un campo de texto
      if (!S.mouse) return { on: false };
      const hit = document.elementFromPoint(S.mouse.x, S.mouse.y);
      return { on: true, x: Math.round(S.mouse.x), y: Math.round(S.mouse.y), text: !!(hit && closest(hit, TEXT_SEL)), numeric: isNumeric(hit && closest(hit, TEXT_SEL)), label: S.mouse.label };
    }
    return { error: `mouse: ${what}?` };
  }

  // ------------------------------------------------------------ rendimiento
  const PERF_CSS = `
*,*::before,*::after{transition-duration:1ms!important;transition-delay:0s!important;
  animation-duration:1ms!important;animation-delay:0s!important;animation-iteration-count:1!important;
  scroll-behavior:auto!important;backdrop-filter:none!important;-webkit-backdrop-filter:none!important;
  background-attachment:scroll!important}
html{cursor:none}`;
  function addPerfStyle() {
    if (!C.reduce_effects || document.getElementById('tcn-perf')) return;
    const st = document.createElement('style');
    st.id = 'tcn-perf';
    st.textContent = PERF_CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  // ------------------------------------------------------------ API
  const API = { ctx, nav, activate, back, scroll, type, clear, go, submit, hint, toast, playPause, dump, mouse, clicked,
    hide: () => { setCur(null); return {}; },
    viewport: () => ({ w: vw(), h: vh() }) };

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

  function destroy() {
    hideRing(); closeGo();
    for (const [t, ty, fn, o] of S.listeners) { try { t.removeEventListener(ty, fn, o); } catch (e) { /* nada */ } }
    for (const id of ['tcn-ring', 'tcn-toast', 'tcn-hint', 'tcn-cursor', 'tcn-style', 'tcn-perf']) { const e = document.getElementById(id); if (e) e.remove(); }
  }

  window.__tcnav = { v: V, cmd, destroy };
  const onMove = () => { follow(250); if (S.mouse) drawCursor(); };
  on(document, 'scroll', onMove, { capture: true, passive: true });
  on(window, 'resize', onMove, { passive: true });
  on(document, 'fullscreenchange', () => { if (S.cur) showRing(); });
  // La página pone el foco en un campo (el buscador de Google, al abrir): el marco va ahí, para que se vea que los
  // números ya escriben.
  on(document, 'focusin', (ev) => {
    const el = ev.target;
    if (S.mouse || !el || el === S.cur || isGoBox(el) || !matches(el, TEXT_SEL) || !usable(el, { near: false })) return;
    setCur(el, false);
  }, { capture: true });
  on(window, 'load', () => {
    const el = editable();
    if (el && !S.mouse && !S.cur && !isGoBox(el) && usable(el, { near: false })) setCur(el, false);
  });
  if (document.readyState === 'loading') on(document, 'DOMContentLoaded', addPerfStyle); else addPerfStyle();
  // Los enlaces que abren «otra ventana»: en pantalla completa no hay otra ventana, van en esta.
  on(document, 'click', (ev) => {
    if (S.armed && ev.target && (ev.target === S.armed.el || S.armed.el.contains(ev.target))) S.armed.hit = true;
    const a = ev.target && ev.target.closest ? ev.target.closest('a[target]') : null;
    if (a && /^_blank$/i.test(a.getAttribute('target') || '')) a.removeAttribute('target');
  }, { capture: true });
})();
