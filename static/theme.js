(() => {
  const STORAGE_KEY = 'cardscanner-theme-v1';
  const themes = [
    { id: 'dark', label: 'Dark' },
    { id: 'midnight', label: 'Midnight Blue' },
    { id: 'forest', label: 'Forest' },
    { id: 'light', label: 'Light' },
  ];
  const valid = new Set(themes.map(theme => theme.id));
  let saved = 'midnight';
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && valid.has(stored)) saved = stored;
  } catch (_) { /* Storage can be unavailable in private browsing. */ }
  const applyTheme = theme => {
    const value = valid.has(theme) ? theme : 'midnight';
    document.body.dataset.theme = value;
    try { localStorage.setItem(STORAGE_KEY, value); } catch (_) { /* Keep the current-page selection. */ }
  };
  document.body.dataset.theme = saved;
  const select = document.querySelector('[data-theme-select]');
  if (!select) return;
  themes.forEach(theme => {
    const option = document.createElement('option');
    option.value = theme.id;
    option.textContent = theme.label;
    select.appendChild(option);
  });
  select.value = saved;
  select.addEventListener('change', () => applyTheme(select.value));
})();
