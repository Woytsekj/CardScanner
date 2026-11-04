(() => {
  const STORAGE_KEY = 'cardscanner-theme-v1';
  const themes = [
    { id: 'dark', label: 'Dark' },
    { id: 'midnight', label: 'Midnight Blue' },
    { id: 'forest', label: 'Forest' },
    { id: 'light', label: 'Light' },
  ];
  const valid = new Set(themes.map(theme => theme.id));
  let saved = 'dark';
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored && valid.has(stored)) saved = stored;
  } catch (_) { /* Storage can be unavailable in private browsing. */ }
  document.body.dataset.theme = saved;

  const picker = document.createElement('div');
  picker.className = 'theme-picker';
  picker.innerHTML = '<label for="cardScannerTheme"><span>Theme</span><select id="cardScannerTheme" aria-label="Choose color theme"></select></label>';
  const select = picker.querySelector('select');
  themes.forEach(theme => {
    const option = document.createElement('option');
    option.value = theme.id;
    option.textContent = theme.label;
    select.appendChild(option);
  });
  select.value = saved;
  select.addEventListener('change', () => {
    const theme = valid.has(select.value) ? select.value : 'dark';
    document.body.dataset.theme = theme;
    try { localStorage.setItem(STORAGE_KEY, theme); } catch (_) { /* Keep the current-page selection. */ }
  });

  const topbar = document.querySelector('.topbar');
  if (topbar) {
    const actions = topbar.querySelector(':scope > .actions, :scope > .site-nav-menu');
    if (actions) topbar.insertBefore(picker, actions);
    else topbar.appendChild(picker);
  }
})();
