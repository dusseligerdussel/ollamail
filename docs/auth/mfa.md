# Zwei-Faktor-Authentifizierung für lokale Konten (#96)

Lokale Konten (E-Mail-Adresse und Passwort in ollamail) – vor allem der Erst-Admin – lassen sich
mit einem zweiten Faktor absichern. Konten, die sich über einen Identity-Provider (OIDC, GitHub,
LDAP) anmelden, nutzen die Anmeldung und die Zwei-Faktor-Regeln ihres Providers; für sie ist
diese Seite ohne Bedeutung.

| Methode | Bibliothek | Gespeichert |
|---|---|---|
| Passkey (WebAuthn) – zweiter Faktor oder Anmeldung ohne Passwort | [`webauthn`](https://github.com/duo-labs/py_webauthn) | Credential-ID, öffentlicher Schlüssel, Signaturzähler, Name (`auth_mfa_passkeys`) |
| Authenticator-App (TOTP, RFC 6238: 6 Ziffern, 30 s, SHA-1) | [`pyotp`](https://github.com/pyauth/pyotp), QR-Code lokal mit [`segno`](https://github.com/heuer/segno) | Secret verschlüsselt (`EncryptedStr`), zuletzt verwendeter Zeitschritt (`auth_mfa_totp`) |
| Wiederherstellungscodes (10 Stück, je ~49 Bit) | – | nur HMAC-SHA256 mit einem aus `OLLAMAIL_SECRET_KEY` abgeleiteten Schlüssel, Zeitpunkt der Verwendung (`auth_mfa_recovery_codes`) |

Alles hängt per `ON DELETE CASCADE` am Nutzer und verschwindet mit ihm.

## Einrichten (Einstellungen → Sicherheit)

- **Passkey hinzufügen:** Name vergeben, dann fragt der Browser nach Fingerabdruck, Gesicht,
  Geräte-PIN oder Sicherheitsschlüssel. Discoverable Credentials werden bevorzugt, damit der
  Passkey auch ohne Passwort funktioniert. Attestation wird nicht angefordert (`none`).
- **Authenticator-App:** QR-Code scannen (oder den Schlüssel abtippen) und mit einem Code
  bestätigen. Erst dann ist TOTP aktiv.
- Mit dem **ersten** Faktor entstehen die **Wiederherstellungscodes**. Sie werden genau einmal
  angezeigt (Kopieren, Herunterladen). „Neue Codes“ ersetzt alle bisherigen.
- Entfernen geht jederzeit, außer es wäre der letzte Faktor und die Zwei-Faktor-Pflicht gilt für
  das Konto (409 `mfa-required`). Ohne Faktor werden die Wiederherstellungscodes gelöscht.

## Anmeldung

1. `POST /api/auth/login` prüft wie bisher Sperren und Passwort. Hat das Konto einen Faktor,
   antwortet es mit **202** (`MfaChallenge`: `status`, `methods`, `expires_at`) **ohne Session**.
2. Statt einer Session setzt es das Cookie `ollamail_mfa` (256 Bit Zufall, in der DB nur der
   SHA-256; `HttpOnly`, `Secure`, `SameSite=Strict`). Der Zwischenzustand gehört zu genau diesem
   Nutzer und diesem Login, gilt `OLLAMAIL_AUTH_MFA_PENDING_MINUTES` (Standard 5) und wird beim
   Abschluss, bei einem neuen Login im selben Browser und nach 5 falschen Codes gelöscht. Danach
   muss das Passwort erneut eingegeben werden (401 `mfa-expired`).
3. Zweiter Schritt: `POST /api/auth/mfa/verify` (`totp` oder `recovery`) bzw.
   `POST /api/auth/mfa/verify/passkey/options` und `POST /api/auth/mfa/verify/passkey`. Erst
   danach entstehen Session und CSRF-Token (`auth.login_succeeded` mit `mfa: <methode>`).

**Ohne Passwort:** Die Login-Seite bietet „Mit Passkey anmelden“, wenn Passkeys konfiguriert
sind (`GET /api/auth/providers` → `passkey_login`). `POST /api/auth/passkey/options` liefert eine
Challenge ohne Credential-Liste (der Browser bietet die Passkeys dieser Seite an),
`POST /api/auth/passkey/login` verlangt **User Verification** (Biometrie/PIN) und zählt damit als
zwei Faktoren. Nur für aktive Konten mit lokalem Passwort und nur bei eingeschalteter lokaler
Anmeldung.

**Schutz gegen Raten:** Für den zweiten Schritt gelten das IP-Limit des Logins
(`OLLAMAIL_AUTH_IP_MAX_ATTEMPTS`) und eine eigene Kontosperre mit denselben Werten wie die
Passwortsperre (`OLLAMAIL_AUTH_LOGIN_MAX_ATTEMPTS` je `OLLAMAIL_AUTH_LOGIN_WINDOW_MINUTES`,
danach 429 mit `retry_after`), zusätzlich höchstens 5 Versuche je Zwischenzustand. Ein
TOTP-Code wird für den aktuellen Zeitschritt und je einen davor/danach akzeptiert, jeder
Zeitschritt nur einmal (kein Replay). Passkeys prüfen RP-ID, Origin, Challenge (einmalig),
Signatur und Signaturzähler. Fehlversuche stehen im Audit-Log (`auth.login_failed`, Akteur
`anonymous`, Ziel = Nutzer-ID, `reason`: `invalid_code`, `locked`, `invalid_passkey`).

## Zwei-Faktor-Pflicht (Admin → Anmeldung)

„Zwei-Faktor-Authentifizierung → Lokale Konten“: **Freiwillig** (Standard), **Pflicht für
Admins** (empfohlen) oder **Pflicht für alle** (`PATCH /api/admin/auth/settings`,
`mfa_enforcement`, Audit `idp.config_changed` mit `kind: mfa`). Konten ohne Faktor richten beim
nächsten Login nach dem Passwort einen ein (202 mit `status: mfa_enrollment_required`); dabei
dürfen sie ausschließlich die Einrichtungs-Endpunkte aufrufen (`/api/auth/mfa/totp/*`,
`/api/auth/mfa/passkeys/options`, `POST /api/auth/mfa/passkeys`). Erst die bestätigte
Einrichtung startet die Session. Bestehende Sessions laufen bis zum nächsten Login weiter.

## Konfiguration

| Variable | Bedeutung |
|---|---|
| `OLLAMAIL_AUTH_WEBAUTHN_RP_ID` | Relying-Party-ID: die Domain der Web-UI, z. B. `mail.example.org`. Leer: aus `OLLAMAIL_AUTH_PUBLIC_URL` |
| `OLLAMAIL_AUTH_WEBAUTHN_ORIGINS` | Erlaubte Origins als JSON-Liste, z. B. `["https://mail.example.org"]`. Leer: `OLLAMAIL_AUTH_PUBLIC_URL` |
| `OLLAMAIL_AUTH_MFA_PENDING_MINUTES` | Gültigkeit des Zwischenzustands nach dem Passwort (1–30, Standard 5) |

RP-ID und Origin kommen nur aus der Konfiguration, nie aus Request-Headern. Ohne beides sind
Passkeys nicht verfügbar (TOTP funktioniert trotzdem). Wer die RP-ID später ändert, macht alle
registrierten Passkeys unbrauchbar; die Betroffenen melden sich dann mit TOTP oder einem
Wiederherstellungscode an.

## Notfallzugang

Hat jemand Passkey, App **und** Wiederherstellungscodes verloren:

```sh
docker compose -f deploy/compose.yaml run --rm api python -m app.cli reset-password --reset-2fa
```

Ohne `--reset-2fa` setzt `reset-password` nur das Passwort und lässt die Faktoren bestehen (mit
Hinweis). Mit der Option werden Passkeys, TOTP und Wiederherstellungscodes gelöscht und im
Audit-Log festgehalten (`auth.mfa_disabled`, Akteur `system`, `via: cli`, nur Anzahlen).
