# Scanner server integration and Cloudflare Zero Trust

The desktop/mobile scanner integration routes are included in this CardScanner v39 repository's `app.py`. The same-origin Android-friendly PWA is served at `/scanner/mobile/`; do not copy a second Flask app or run a separate API service for it.

## Deploy

1. Back up the existing `data/` directory and database before updating.
2. Deploy the complete repository, including `scanner-client/mobile-app/` and the updated `app.py`.
3. Rebuild and restart the web container:

   ```bash
   docker compose up -d --build web
   docker compose logs --tail=150 web
   ```

4. Open `https://YOUR-CARDS-HOST/scanner/mobile/` in Chrome on Android, sign in to Cloudflare Access if prompted, then sign in to the CardScanner application account. In Chrome, use **Install app** / **Add to Home screen**.
5. Confirm that a user can only see and scan to the person mapped to their application account. Test with a normal account, not the administrator account.

## Security and account mapping

- Cloudflare Access should protect the host/path using a policy limited to you and invited friends. A tunnel alone does not authenticate users. Do not create a bypass for `/scanner/mobile/` or `/api/scanner/mobile/*`. If you configured a path-scoped Access application rather than protecting the whole hostname, include a separate matching Access application/policy for `/api/scanner/mobile/*`; the mobile API must not be left outside the intended Access boundary. Cloudflare documents path matching and specificity in [Application paths](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/app-paths/).
- Access is an outer access-control layer. The PWA also uses the normal CardScanner Flask session and CSRF token. Users therefore authenticate through Cloudflare Access and then sign in to CardScanner itself unless your deployment has a separately configured, trusted SSO bridge.
- The Android PWA intentionally contains **no** scanner API key and no Cloudflare service-token secret. Browser requests use same-origin cookies and the per-session CSRF token.
- The existing `users.person_id` model links one login account to at most one default person. Non-admin scanner sessions are restricted server-side to that linked person. Admin accounts can choose any active person. If you need multiple people selectable under one login, that requires a separate account-to-people schema and admin UI; this release does not pretend the current one-to-one mapping is many-to-many.
- Keep `SCANNER_API_KEY` private to trusted desktop scanner installations. It allows access to low-scope scanner endpoints, including scanning to any active person. Do not put it in the PWA or share it with friends. The desktop client can optionally use Cloudflare Access service-token environment variables, but those secrets must stay on the machine running the desktop client.
- A friend must have an active CardScanner web account linked to an active person in Admin. A Cloudflare Access identity alone does not create an application account or inventory authorization.

## Routes provided

| Route | Purpose | Auth |
|---|---|---|
| `GET /api/scanner/options` | Desktop scanner people/account/location options | `X-Scanner-API-Key` |
| `GET /api/scanner/mobile/options` | Per-session authorized people and observed storage locations | Flask login session |
| `GET /api/scanner/mobile/price/<scryfall_id>` | Server-side current card price lookup for PWA | Flask login session |
| `POST /api/scanner/mobile/scan` | Validated scan write from Android PWA | Flask login session + CSRF |
| `GET /scanner/mobile/` | PWA shell and install manifest entrypoint | Flask login session |

The legacy `GET /api/people`, `POST /api/scan`, and batch scan endpoints remain available for compatible desktop clients.

## Inventory semantics

- The existing `inventory.location` text field remains the physical storage location; this app does not have a dedicated storage-location table. Options are derived from observed inventory location labels plus `Free`.
- `inventory.for_vending` is independent of physical location. Scan payloads accept the compatible `is_vending` / `vending` flags, and the server persists the canonical `for_vending` field.
- `person_id` and `account_id` from clients are validated against the server's active people/accounts. For session-authenticated mobile scans, the server selects the owner label from the authorized database person instead of trusting the submitted name.
- A scan marked for vending creates or increments a separate unassigned inventory row in the selected location/vending bucket. It does not modify a row assigned to a deck. Existing bulk-update/deck operations continue to enforce the deck/vending conflict.
- The desktop scanner uses a stable `scan_id` for retries so a request whose response was lost is not added twice.

## Troubleshooting

- **Options API gives 401 in desktop app:** check `MTG_SERVER_URL`, `MTG_SCANNER_API_KEY`, and the server's `SCANNER_API_KEY`; then verify that Cloudflare Access permits that client authentication method.
- **Mobile page says the account is not mapped:** in CardScanner Admin, link the user's login to an active person, then sign out/in and reload the PWA.
- **Camera does not open:** use the HTTPS hostname, grant camera permission in Android Chrome, and retry. The PWA camera is intentionally disabled on other application pages by the site's Permissions-Policy.
- **A save is rejected:** review the JSON error on screen. Refresh options if the person's account mapping changed. Do not bypass server validation by editing the client payload.
- **Tesseract OCR does not load:** type the name manually and select the exact Scryfall printing. OCR is only a suggestion; the app does not auto-save an OCR guess.
