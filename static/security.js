(() => {
  'use strict';
  const meta = document.querySelector('meta[name="csrf-token"]');
  const token = meta ? meta.getAttribute('content') : '';
  const unsafe = new Set(['POST','PUT','PATCH','DELETE']);

  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const method = String(init.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    let url = '';
    try { url = new URL(typeof input === 'string' ? input : input.url, location.href).href; } catch (_) { return originalFetch(input, init); }
    const target = new URL(url, location.href);
    if (unsafe.has(method) && target.origin === location.origin && token) {
      const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
      headers.set('X-CSRFToken', token);
      init = Object.assign({}, init, {headers});
    }
    return originalFetch(input, init);
  };

  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    const method = String(form.method || 'get').toUpperCase();
    if (!unsafe.has(method) || !token) return;
    if (!form.querySelector('input[name="csrf_token"]')) {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'csrf_token';
      input.value = token;
      form.appendChild(input);
    }
  }, true);
})();
