# Zwei-Faktor-Authentifizierung für lokale Konten (#96)

Lokale Konten (E-Mail-Adresse und Passwort in ollamail) – vor allem der Erst-Admin – lassen sich
mit einem zweiten Faktor absichern. Konten, die sich über einen Identity-Provider (OIDC, GitHub,
LDAP) anmelden, nutzen die Anmeldung und die Zwei-Faktor-Regeln ihres Providers; für sie ist
diese Seite ohne Bedeutung.

| Methode | Bibliothek | Gespeichert |
|---|---|---|
| Passkey (WebAuthn) – zweiter Faktor oder Anmeldung ohne Passwort | [`webauthn`](https://github.com/duo-labs/py_webauthn) | Credential-ID, öffentlicher Schlüssel, Signaturzähler, Name (`auth_mfa_passkeys`) |
| Authenticator-App (TOTP, RFC 6238: 6 Ziffern, 30 s, SHA-1) | [`pyotp`](https://github.com/pyauth/pyotp), QR-Code lokal mit [`segno`](https://github.com/heuer/segno) | Secret verschlüsselt (`EncryptedStr`), zuletzt verwendeter Zeitschritt (`auth_mfa_totp`) |
| Wiederherstellungscodes (10 Stück, je ~49 Bit) | – | nur HMAC-SHA256 mit einem aus `OLLAMAIL_SECRET_KEY` abgeleiteten Schlüssel, Zeitpunkt der Verwendung (`auth_mfa_recovery_codes`); nach einer Key-Rotation siehe [unten](#wiederherstellungscodes-und-key-rotation) |

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

**Einladung und Registrierung** (#144) laufen über denselben Schrittfluss: Nach dem Festlegen
des Passworts (`POST /api/auth/invitations/accept`) bzw. der Selbstregistrierung
(`POST /api/auth/register`) gibt es bei geltender Pflicht **202** (`MfaChallenge`,
`mfa_enrollment_required`) und noch keine Session; hat das Konto bereits einen Faktor,
`mfa_required`. Das Passwort ist dann schon gespeichert, die Einladung verbraucht. Die
Einladungsseite zeigt anschließend dieselben Schritte wie die Login-Seite.

## Bestätigung vor sensiblen Aktionen (#144)

Eine gestohlene Session soll nicht reichen, um die Zwei-Faktor-Authentifizierung abzuschalten
oder das Konto zu löschen. Diese Endpunkte verlangen deshalb eine **aktuelle Bestätigung**:

| Aktion | Endpunkt |
|---|---|
| Authenticator-App entfernen | `DELETE /api/auth/mfa/totp` |
| Passkey entfernen | `DELETE /api/auth/mfa/passkeys/{id}` |
| Neue Wiederherstellungscodes | `POST /api/auth/mfa/recovery-codes` |
| Vollständiger Datenexport | `POST /api/privacy/exports` |
| Konto löschen | `DELETE /api/privacy/account` (zusätzlich Eingabe der E-Mail-Adresse) |

Dasselbe gilt für **kritische Admin-Aktionen** (#190, #206), denn ein gestohlenes Admin-Cookie
reicht sonst, um einen Identity-Provider mit `link_by_email` anzulegen, der sich als beliebiger
Nutzer anmeldet, oder einen KI-Endpunkt auf einen fremden Server umzubiegen. Sie hängen an
`RecentAdminDep` (erst Admin-Prüfung, dann Bestätigung):

| Aktion | Endpunkt |
|---|---|
| Nutzer löschen | `DELETE /api/admin/privacy/users/{id}` |
| Rolle ändern, deaktivieren/reaktivieren | `PATCH /api/users/{id}` |
| SCIM-Token erstellen | `POST /api/admin/scim/tokens` |
| OIDC-, GitHub-, SAML-Provider anlegen/ändern | `POST /api/admin/auth/{oidc,github,saml}/providers`, `PATCH …/providers/{name}` |
| LDAP-Verzeichnis anlegen/ändern | `POST /api/auth/ldap/directories`, `PUT …/{name}` |
| Anmelde-Einstellungen (lokale Anmeldung, 2FA-Pflicht) | `PATCH /api/admin/auth/settings` |
| KI-Provider anlegen/ändern | `POST /api/admin/ai/providers`, `PATCH /api/admin/ai/providers/{name}` |
| Verbindungstest mit gespeichertem API-Key an eine andere URL oder einen anderen Typ (#219) | `POST /api/admin/ai/providers/test` (ohne `api_key`, mit `name`) |
| Rollen-Zuordnung (Gruppe → Rolle) speichern (#206) | `PUT /api/admin/auth/role-mapping` |
| SCIM-Einstellungen (Schalter, verknüpfende Provider) (#206) | `PATCH /api/admin/scim` |
| KI-Einstellungen: Cloud-Provider einschalten oder Aufgaben zuordnen (#206) | `PATCH /api/admin/ai/settings` |
| Shared-Mailbox-Zuweisungen ändern (#206) | `PUT /api/admin/shared-mailboxes/{id}/assignments`; beim Anlegen (`POST /api/admin/shared-mailboxes`) nur, wenn Zuweisungen mitgeschickt werden |

Lesen, Verbindungstests (außer dem gespeicherten Key an einem neuen Ziel), Löschen von Providern und „Überall abmelden“ brauchen keine
Bestätigung: Sie geben niemandem Zugang zu fremden Konten oder Mails. Bei den KI-Einstellungen
gilt das auch für das Ausschalten der Cloud-Provider, das Profil und die Parallelität: Damit
gehen keine Mail-Inhalte an einen anderen Endpunkt. Wer sich selbst ein Shared Mailbox zuweist,
kann es lesen – deshalb hängen die Zuweisungen ebenfalls an der Bestätigung (zusätzlich zum
Audit-Log).

Jede Session speichert `authenticated_at` (Anmeldung oder letzte Bestätigung). Liegt das länger
als `OLLAMAIL_AUTH_REAUTH_MINUTES` (Standard 10) zurück, antworten die Endpunkte mit **403**
`urn:ollamail:problem:reauth-required` (`reauth_minutes`). Die Web-UI öffnet dann das Sheet
„Bestätige, dass du es bist“ und führt die Aktion nach der Bestätigung erneut aus; Abbrechen
ändert nichts. Sessions von vor dem Update gelten ab ihrem Anmeldezeitpunkt.

`GET /api/auth/reauth` nennt die Möglichkeiten des Kontos (`methods`) und wie lange die
aktuelle Bestätigung noch gilt (`valid_until`):

| Methode | Für | Endpunkt |
|---|---|---|
| `password` | Konten mit lokalem Passwort | `POST /api/auth/reauth` (`{"method": "password", "password": …}`) |
| `webauthn` | Konten mit Passkey (und konfigurierter RP-ID) | `POST /api/auth/reauth/passkey/options`, dann `POST /api/auth/reauth/passkey` |
| `totp` | Konten mit Authenticator-App | `POST /api/auth/reauth` (`{"method": "totp", "code": …}`), jeder Zeitschritt nur einmal |
| `sso` | Sessions eines Redirect-Providers (OIDC, GitHub, SAML), solange er aktiv ist | erneute Anmeldung über `login_path` mit `return_to`; die neue Session beginnt mit frischem `authenticated_at` |
| `signin` | alle (z. B. LDAP-Konten) | abmelden und neu anmelden, danach zurück zur Seite |

Wiederherstellungscodes zählen bewusst nicht: Sie sind der letzte Ausweg bei verlorenem Faktor
und sollen nicht nebenbei verbraucht werden.

**Warum SSO-Konten sich beim Provider neu anmelden:** ollamail kennt für sie weder Passwort noch
zweiten Faktor (die verwaltet der Identity-Provider). Die erneute Anmeldung beweist, dass der
Browser eine gültige Sitzung beim Provider hat bzw. dessen Anmeldung (inkl. seines zweiten
Faktors) besteht – ein gestohlenes ollamail-Session-Cookie allein bringt das nicht mit. Ein
eigener zweiter Faktor in ollamail für SSO-Konten würde die Faktorverwaltung doppeln.

**Schutz gegen Raten:** Bestätigungsversuche zählen je Konto mit den Werten der Login-Sperre
(`OLLAMAIL_AUTH_LOGIN_MAX_ATTEMPTS` je `OLLAMAIL_AUTH_LOGIN_WINDOW_MINUTES`, danach 429); die
Session bleibt bestehen. Audit: `auth.reauthenticated` (`method`) und `auth.reauth_failed`
(`method`, `reason`: `invalid_credentials`, `invalid_passkey`), Ziel ist die Session.

## Wiederherstellungscodes und Key-Rotation

Die Codes sind HMACs mit einem aus `OLLAMAIL_SECRET_KEY` abgeleiteten Schlüssel und lassen sich
nicht neu verschlüsseln. Bei der Prüfung werden deshalb auch die Schlüssel aus
`OLLAMAIL_SECRET_KEYS_OLD` versucht; ein so erkannter Code wird als verwendet markiert und mit dem
aktuellen Schlüssel neu gehasht. Codes von vor einer Rotation gelten also, solange der alte Key in
`OLLAMAIL_SECRET_KEYS_OLD` steht. Wer ihn nach `rotate-keys` entfernt
([`deploy/README.md`](../../deploy/README.md#master-key-und-key-rotation)), macht diese Codes
ungültig – vorher die Nutzer neue Codes erzeugen lassen (Einstellungen → Sicherheit → „Neue
Codes“) oder den alten Key dort stehen lassen.

## Konfiguration

| Variable | Bedeutung |
|---|---|
| `OLLAMAIL_AUTH_WEBAUTHN_RP_ID` | Relying-Party-ID: die Domain der Web-UI, z. B. `mail.example.org`. Leer: aus `OLLAMAIL_AUTH_PUBLIC_URL` |
| `OLLAMAIL_AUTH_WEBAUTHN_ORIGINS` | Erlaubte Origins als JSON-Liste, z. B. `["https://mail.example.org"]`. Leer: `OLLAMAIL_AUTH_PUBLIC_URL` |
| `OLLAMAIL_AUTH_MFA_PENDING_MINUTES` | Gültigkeit des Zwischenzustands nach dem Passwort (1–30, Standard 5) |
| `OLLAMAIL_AUTH_REAUTH_MINUTES` | Wie lange eine Bestätigung vor sensiblen Aktionen gilt (1–1440, Standard 10) |

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
