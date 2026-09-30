/* ME Vendor Hub: small client helpers around HTMX.
   Server responses send HX-Trigger events: toast, refresh, drawerReload, closeModal, openDrawer. */
(function () {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);

  function toast(msg, tone) {
    const box = $('#toasts'); if (!box) return;
    const el = document.createElement('div');
    el.className = 'toast';
    el.setAttribute('role', 'status');
    const ic = tone === 'bad' ? 'alert' : tone === 'info' ? 'info' : 'check';
    el.innerHTML = `<svg class="i ${tone === 'ok' || !tone ? 'ok' : ''}" viewBox="0 0 24 24">${ICONS[ic]}</svg><span></span><button type="button" aria-label="Dismiss"><svg class="i" viewBox="0 0 24 24">${ICONS.x}</svg></button>`;
    el.querySelector('span').textContent = msg;
    el.querySelector('button').onclick = () => el.remove();
    box.appendChild(el);
    setTimeout(() => el.remove(), 5000);
  }
  const ICONS = {
    check: '<path d="M20 6 9 17l-5-5"/>', alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4M12 8h.01"/>', x: '<path d="M18 6 6 18M6 6l12 12"/>',
  };

  function busy() { return !!(document.activeElement && document.activeElement.matches('input,select,textarea')); }

  function reloadView() {
    const v = $('#view'); if (!v) return;
    htmx.ajax('GET', location.pathname + location.search, { target: '#view', select: '#view', swap: 'outerHTML' });
  }
  function reloadNav() {
    htmx.ajax('GET', '/nav/?path=' + encodeURIComponent(location.pathname), { target: '#side', swap: 'innerHTML' });
    htmx.ajax('GET', '/topbar/', { target: '#bell-slot', swap: 'innerHTML' });
  }
  function reloadDrawer() {
    const d = $('#drawer [data-url]'); if (!d) return;
    htmx.ajax('GET', d.dataset.url, { target: '#drawer', swap: 'innerHTML' });
  }
  function closeDrawer() { $('#drawer').innerHTML = ''; }
  function closeModal() { $('#modal').innerHTML = ''; }

  document.addEventListener('toast', e => toast(e.detail.msg, e.detail.tone));
  document.addEventListener('refresh', () => { reloadView(); reloadNav(); });
  document.addEventListener('drawerReload', reloadDrawer);
  // Deferred so the other HX-Trigger events (openDrawer, toast) still bubble from the element inside the modal.
  document.addEventListener('closeModal', () => setTimeout(closeModal, 0));
  document.addEventListener('openDrawer', e => htmx.ajax('GET', e.detail.url, { target: '#drawer', swap: 'innerHTML' }));
  // An autosaving form that stays open learns its record's new version (optimistic locking).
  document.addEventListener('version', e => { const el = document.getElementById(e.detail.id); if (el) el.value = e.detail.v; });

  // Mobile menu button (no inline handlers: the Content-Security-Policy allows only script files).
  document.addEventListener('click', e => {
    if (e.target.closest('[data-toggle="menu"]')) $('#app').classList.toggle('side-open');
  });

  document.addEventListener('click', e => {
    const t = e.target.closest('[data-close]');
    if (!t) return;
    if (t.dataset.close === 'drawer') closeDrawer();
    if (t.dataset.close === 'modal') closeModal();
    if (t.dataset.close === 'pop') $('#pop').innerHTML = '';
    if (t.dataset.close === 'pal') $('#pal').innerHTML = '';
    if (t.dataset.close === 'menu') $('#app').classList.remove('side-open');
  });

  document.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault();
      if ($('#pal').innerHTML) { $('#pal').innerHTML = ''; return; }
      htmx.ajax('GET', '/search/', { target: '#pal', swap: 'innerHTML' });
      return;
    }
    if (e.key === 'Escape') {
      for (const id of ['#pal', '#pop', '#modal', '#drawer']) { if ($(id).innerHTML.trim()) { $(id).innerHTML = ''; return; } }
    }
  });

  // Command palette keyboard navigation
  document.addEventListener('keydown', e => {
    if (!e.target.matches('#pal-in')) return;
    const items = [...document.querySelectorAll('#pal-r .pal-i')];
    let i = items.findIndex(x => x.classList.contains('on'));
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      i = e.key === 'ArrowDown' ? Math.min(items.length - 1, i + 1) : Math.max(0, i - 1);
      items.forEach((x, k) => x.classList.toggle('on', k === i));
      items[i] && items[i].scrollIntoView({ block: 'nearest' });
    }
    if (e.key === 'Enter' && items.length) { e.preventDefault(); (items[i >= 0 ? i : 0]).click(); }
  });

  // Drag and drop for the upload dropzone
  document.addEventListener('dragover', e => { const z = e.target.closest && e.target.closest('.drop'); if (z) { e.preventDefault(); z.classList.add('over'); } });
  document.addEventListener('dragleave', e => { const z = e.target.closest && e.target.closest('.drop'); if (z) z.classList.remove('over'); });
  document.addEventListener('drop', e => {
    const z = e.target.closest && e.target.closest('.drop'); if (!z) return;
    e.preventDefault(); z.classList.remove('over');
    const input = z.querySelector('input[type=file]');
    if (input && e.dataTransfer.files.length) { input.files = e.dataTransfer.files; input.dispatchEvent(new Event('change', { bubbles: true })); }
  });

  // Buttons and inputs inside a clickable row act on their own; the row must not open the drawer too.
  document.addEventListener('htmx:confirm', e => {
    const el = e.detail.elt, ev = e.detail.triggeringEvent, t = ev && ev.target;
    if (!t || !el.matches('tr.click, .g-row')) return;
    const inner = t.closest('button, a, input, select, textarea, label');
    if (inner && inner !== el && el.contains(inner)) e.preventDefault();
  });

  // A drawer or dialog that is already open is re-rendered in place (tab switch, next wizard step, reload):
  // skip the opening animation so the content does not flash.
  document.addEventListener('htmx:beforeSwap', e => {
    const t = e.detail.target;
    if (!t || !(t.id === 'modal' || t.id === 'drawer') || !t.innerHTML.trim() || typeof e.detail.serverResponse !== 'string') return;
    e.detail.serverResponse = e.detail.serverResponse
      .replace('class="modal-w"', 'class="modal-w still"')
      .replace('class="scrim"', 'class="scrim still"')
      .replace('<aside class="drawer', '<aside class="still drawer');
  });

  // Copy buttons
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-copy]'); if (!b) return;
    const el = document.getElementById(b.dataset.copy);
    const done = () => toast('Copied to clipboard', 'ok');
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(el.value || el.textContent).then(done, () => { el.select(); });
    else el.select();
  });

  // Live updates: the server sends "changed" when any record changes (SSE).
  function connect() {
    if (!window.EventSource || !document.body.dataset.sse) return;
    const es = new EventSource('/events/');
    es.addEventListener('changed', () => {
      reloadNav();
      if (!busy() && !$('#modal').innerHTML.trim() && !$('#drawer').innerHTML.trim()) reloadView();
    });
  }
  document.addEventListener('DOMContentLoaded', connect);

  // A shared record link lands on its list page with ?open=/records/...: open that drawer.
  document.addEventListener('DOMContentLoaded', () => {
    const url = new URLSearchParams(location.search).get('open');
    if (url && url.startsWith('/records/')) htmx.ajax('GET', url, { target: '#drawer', swap: 'innerHTML' });
  });

  // CSRF for every HTMX request
  document.addEventListener('htmx:configRequest', e => {
    const t = document.querySelector('meta[name=csrf-token]');
    if (t) e.detail.headers['X-CSRFToken'] = t.content;
  });
  window.hub = { toast, reloadView, reloadNav, closeDrawer, closeModal };
})();
