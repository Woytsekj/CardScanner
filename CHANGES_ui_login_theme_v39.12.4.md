# CardScanner v39.12.4 — Theme selector placement

- Removed the theme selector script from the login page.
- Removed the theme.js fallback that injected the selector into `.auth-card`; theme selection is now only injected into authenticated application pages with a top navigation bar.
- Kept saved theme preferences and all existing themes unchanged after sign-in.
- Bumped static asset cache-busting query versions to v39.12.4.
