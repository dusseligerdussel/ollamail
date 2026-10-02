# Anmeldung mit GitHub

Entwicklerteams und kleine Organisationen melden sich mit ihrem GitHub-Konto an, auf github.com
oder auf einem eigenen **GitHub Enterprise Server** (GHES). Die Anmeldung lässt sich auf
Mitglieder bestimmter **Organisationen** oder **Teams** beschränken. Beim ersten Login wird das
ollamail-Konto automatisch angelegt (Just-in-Time-Provisioning wie bei OIDC). Mehrere
GitHub-Provider können gleichzeitig aktiv sein, z. B. github.com und ein GHES.

Inhalt:

1. [Überblick](#1-überblick)
2. [Voraussetzungen](#2-voraussetzungen)
3. [OAuth App anlegen](#3-oauth-app-anlegen)
4. [Alternative: GitHub App](#4-alternative-github-app)
5. [GitHub Enterprise Server](#5-github-enterprise-server)
6. [Admin-API](#6-admin-api)
7. [Organisationen, Teams und Gruppen](#7-organisationen-teams-und-gruppen)
8. [E-Mail-Adresse, Kontoanlage und Verknüpfung](#8-e-mail-adresse-kontoanlage-und-verknüpfung)
9. [Fehlercodes auf der Login-Seite](#9-fehlercodes-auf-der-login-seite)
10. [Sicherheit und Datenschutz](#10-sicherheit-und-datenschutz)

## 1. Überblick

```
Browser ── GET /api/auth/github/<name>/login ──▶ ollamail ── 303 ──▶ GitHub (Login, Consent)
Browser ◀─────────────────────────── 302 ──────────────────────────── GitHub
Browser ── GET /api/auth/github/<name>/callback?code&state ──▶ ollamail
          ollamail ── Code + Client-Secret + PKCE-Verifier ──▶ GitHub (/login/oauth/access_token)
          ollamail ── REST-API mit dem Token des Nutzers: /user, /user/teams,
                      /user/memberships/orgs/<org>, /user/emails
          ollamail prüft Organisation/Team und E-Mail, legt Konto an, startet Session
Browser ◀── 303 zurück in die App (Session-Cookie) ── ollamail
```

- OAuth 2.0 Authorization Code Flow mit `state` und **PKCE (S256)**. Der Flow (verschlüsseltes
  Einmal-Cookie, Fehler-Redirects, Session) ist derselbe wie bei OIDC
  (`backend/app/auth/redirect_flow.py`). GitHub kennt kein ID-Token, deshalb gibt es keinen `nonce`.
- Das Access-Token von GitHub wird nur während des Callbacks benutzt und weder gespeichert noch
  geloggt. Danach gilt die normale serverseitige Session von ollamail.
- Die Login-Seite fragt `GET /api/auth/providers` ab. Jeder aktive GitHub-Provider erscheint dort
  mit `name` `github:<name>` und `login_path` `/auth/github/<name>/login`; der Button heißt wie
  `display_name` (Standard „GitHub“).
- Code: `backend/app/auth/providers/github/`.

## 2. Voraussetzungen

- ollamail ist per **HTTPS** erreichbar, und `OLLAMAIL_AUTH_PUBLIC_URL` ist gesetzt (wie bei OIDC,
  siehe [`oidc.md`](oidc.md) §2). Daraus entsteht die **Callback-URL**, die bei GitHub
  eingetragen wird:

  ```
  https://mail.example.org/api/auth/github/<name>/callback
  ```

  `<name>` ist der Kurzname des Providers in ollamail (z. B. `github`). Die Admin-API zeigt die
  fertige URL im Feld `redirect_uri` an.
- Es gibt keine neuen Umgebungsvariablen. GitHub-Provider werden ausschließlich über die
  Admin-API gepflegt (verschlüsselt in der Datenbank). Eine Oberfläche dafür folgt mit #33.

## 3. OAuth App anlegen

1. GitHub → *Settings* → *Developer settings* → *OAuth Apps* → *New OAuth App*. Für eine
   Organisation besser unter *Organisation* → *Settings* → *Developer settings* → *OAuth Apps*
   anlegen, damit die App nicht an einer Person hängt.
2. *Application name*: z. B. `ollamail`. *Homepage URL*: `https://mail.example.org`.
3. *Authorization callback URL*: die Callback-URL aus §2.
4. Nach dem Anlegen *Generate a new client secret*. Client-ID und Secret in ollamail eintragen:

   ```bash
   curl -X POST https://mail.example.org/api/admin/auth/github/providers \
     -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" -b "$COOKIES" \
     -d '{
       "name": "github",
       "display_name": "GitHub",
       "client_id": "Ov23li…",
       "client_secret": "…",
       "allowed_organizations": ["acme"]
     }'
   ```

ollamail fordert die Scopes `read:user user:email read:org` an:

| Scope | Wofür |
|---|---|
| `read:user` | Profil (Nutzer-ID, Name) |
| `user:email` | E-Mail-Adressen inkl. privater, um die verifizierte primäre Adresse zu lesen |
| `read:org` | Org- und Team-Mitgliedschaften, auch nicht öffentliche |

**Organisationen mit OAuth-App-Zugriffsbeschränkung:** Hat eine Organisation *Third-party
application access policy* aktiviert, muss ein Org-Owner die App freigeben (*Settings* →
*Third-party access*), sonst meldet GitHub keine Mitgliedschaft und der Login endet mit
`not_member`.

## 4. Alternative: GitHub App

Eine GitHub App funktioniert ebenso (Login mit *user access token*). Scopes werden dabei ignoriert;
stattdessen braucht die App diese Berechtigungen:

- *Account permissions* → *Email addresses*: **Read-only**
- *Organization permissions* → *Members*: **Read-only** (für Org- und Team-Prüfung und Gruppen)

Weitere Einstellungen: *Callback URL* wie in §2, *Request user authorization (OAuth) during
installation* aus, *Expire user authorization tokens* beliebig (das Token wird nur einmal benutzt).
Die App muss in den erlaubten Organisationen **installiert** sein; GitHub meldet Teams und
Mitgliedschaften nur für Organisationen mit Installation. Client-ID und Client-Secret der App
werden wie bei der OAuth App eingetragen.

## 5. GitHub Enterprise Server

`base_url` auf die Adresse des GHES setzen, z. B. `https://github.example.org`. ollamail nutzt dann

- `https://github.example.org/login/oauth/authorize` und `/login/oauth/access_token`,
- die REST-API unter `https://github.example.org/api/v3`.

Die OAuth App wird auf dem GHES angelegt (gleiche Schritte wie in §3). `base_url` muss eine
HTTPS-URL ohne Zugangsdaten, Query oder Fragment sein; ein abschließender `/` wird entfernt.
`https://github.com` (oder `null`) bedeutet github.com. Das TLS-Zertifikat des GHES muss für den
Backend-Container gültig sein (eigene CA ggf. über `SSL_CERT_FILE` einbinden). PKCE wird mitgesendet;
ältere GHES-Versionen ohne PKCE-Unterstützung ignorieren den Parameter, `state` und Client-Secret
schützen den Flow dann weiterhin.

## 6. Admin-API

Alle Endpunkte erfordern die Rolle `admin`. Jede Änderung (anlegen, ändern, löschen) landet im
Audit-Log (`idp.config_changed`, `kind: "github"`, nur ID und Art der Änderung, keine Werte).

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/admin/auth/github/providers` | Alle GitHub-Provider |
| `POST` | `/api/admin/auth/github/providers` | Provider anlegen |
| `GET` | `/api/admin/auth/github/providers/{name}` | Ein Provider |
| `PATCH` | `/api/admin/auth/github/providers/{name}` | Ändern; fehlende Felder bleiben, `base_url: null` wechselt zu github.com |
| `DELETE` | `/api/admin/auth/github/providers/{name}` | Löschen |

Felder:

| Feld | Standard | Bedeutung |
|---|---|---|
| `name` | – | Kurzname, `a-z`, `0-9`, `-`, max. 32 Zeichen; Teil der Callback-URL, nicht änderbar |
| `display_name` | `GitHub` | Text des Login-Buttons |
| `base_url` | `null` | GHES-Adresse; `null` für github.com |
| `client_id` / `client_secret` | – | Zugangsdaten der OAuth App / GitHub App; das Secret ist Pflicht, wird verschlüsselt gespeichert und nie ausgegeben (`has_client_secret`) |
| `enabled` | `true` | Provider auf der Login-Seite anbieten |
| `auto_provision` | `true` | Unbekannte Nutzer beim ersten Login anlegen (Rolle `user`) |
| `link_by_email` | `false` | Vorhandenen Nutzer mit gleicher verifizierter primärer E-Mail-Adresse verknüpfen |
| `allowed_domains` | `[]` | Erlaubte E-Mail-Domains (leer: alle) |
| `allowed_organizations` | `[]` | Nur Mitglieder dieser Organisationen (Login-Namen, Groß-/Kleinschreibung egal) |
| `allowed_teams` | `[]` | Nur Mitglieder dieser Teams, Format `<org>/<team-slug>` |

Löschen oder Deaktivieren lässt Nutzer und ihre verknüpften Identitäten bestehen; wird der Provider
unter demselben Namen für dieselbe GitHub-Instanz neu angelegt, funktionieren die Logins wieder.
Laufende Sessions bleiben bis zu ihrem Ablauf gültig; zum sofortigen Aussperren den Nutzer
deaktivieren.

## 7. Organisationen, Teams und Gruppen

Die Prüfung läuft bei **jedem** Login serverseitig, nach dem Code-Austausch:

- Sind `allowed_organizations` und `allowed_teams` leer, darf jedes GitHub-Konto der Instanz
  sich anmelden (begrenzt nur durch `auto_provision` und `allowed_domains`). Für github.com ist
  das selten gewollt: **mindestens eine Organisation oder ein Team eintragen** oder
  `auto_provision` ausschalten.
- Sonst genügt die Mitgliedschaft in **einer** erlaubten Organisation **oder** einem erlaubten
  Team. Organisationen prüft `GET /user/memberships/orgs/<org>`; nur `state: active` zählt, offene
  Einladungen nicht. Teams kommen aus `GET /user/teams`.
- Können Mitgliedschaften nicht gelesen werden (kein Consent für `read:org`, GitHub App ohne
  *Members*-Berechtigung oder nicht installiert), gilt der Nutzer als Nicht-Mitglied: Der Login
  wird abgelehnt (`not_member`), nie stillschweigend erlaubt.

**Gruppen:** Die Teams des Nutzers werden als `<org>/<team-slug>` (klein geschrieben) in
`auth_identities.groups` gespeichert, bei jedem Login aktualisiert. Ist eine Beschränkung
gesetzt, nur Teams der erlaubten Organisationen (bzw. der Organisationen der erlaubten Teams);
Teams in fremden Organisationen gehen ollamail nichts an. Das Rollen-Mapping (#33) wertet die
Gruppen aus; dieses Issue speichert sie nur.

## 8. E-Mail-Adresse, Kontoanlage und Verknüpfung

- Verwendet wird **nur die primäre E-Mail-Adresse, und nur, wenn GitHub sie als verifiziert
  meldet** (`GET /user/emails`, `primary: true`, `verified: true`). Weitere Adressen werden
  ignoriert, auch wenn sie verifiziert sind. Ohne verifizierte primäre Adresse liefert GitHub für
  ollamail keine Adresse: Ein neuer Nutzer kann sich nicht anmelden (`email_missing`); eine bereits
  verknüpfte Identität meldet sich weiter an, scheitert aber an einer gesetzten Domain-Allowlist.
- Die Identität ist die **numerische GitHub-Nutzer-ID** (`github:<name>` + ID), nicht der
  Login-Name: Umbenennungen bei GitHub ändern nichts, ein freigewordener Login-Name kann nicht
  übernommen werden.
- Kontoanlage, Verknüpfung über `link_by_email`, Domain-Allowlist und Deaktivierung folgen dem
  gemeinsamen Provisioning (`backend/app/auth/provisioning.py`, siehe [`oidc.md`](oidc.md) §10).
  Die E-Mail-Adresse in ollamail wird bei späteren Logins nicht überschrieben.

## 9. Fehlercodes auf der Login-Seite

Bei Fehlern leitet ollamail auf `/login?error=<code>` um. Zusätzlich zu den gemeinsamen Codes aus
[`oidc.md`](oidc.md) §12 (`provider_unknown`, `too_many_attempts`, `state_invalid`, `email_missing`,
`domain_not_allowed`, `email_conflict`, `not_provisioned`, `inactive`):

| Code | Bedeutung |
|---|---|
| `idp_error` | GitHub hat den Login abgebrochen (z. B. Zustimmung verweigert, falsche Callback-URL) |
| `token_exchange_failed` | Code-Einlösung fehlgeschlagen (Client-Secret, Callback-URL, PKCE, Code abgelaufen) |
| `provider_unavailable` | GitHub-API nicht erreichbar oder unerwartete Antwort |
| `not_member` | Nicht Mitglied einer erlaubten Organisation oder eines erlaubten Teams |

Das Log (`login_failed`) enthält Provider, Code und den fehlgeschlagenen Schritt (z. B.
`check=user`), nie Tokens, Login-Namen, Adressen oder Antworten der API.

## 10. Sicherheit und Datenschutz

- **`state` und PKCE:** zufällig je Login, im verschlüsselten Einmal-Cookie des gemeinsamen
  Flows (siehe [`oidc.md`](oidc.md) §13). Der Callback wird ohne passenden `state` verworfen, bevor
  GitHub kontaktiert wird.
- **Serverseitige Prüfung:** Org- und Team-Mitgliedschaft und E-Mail-Verifizierung prüft das
  Backend mit dem Token des Nutzers direkt bei GitHub; nichts davon kommt aus dem Browser.
- **Gespeichert** werden nur die GitHub-Nutzer-ID, die Teams (für das Rollen-Mapping) und beim
  ersten Login E-Mail-Adresse und Name. Access-Token, Login-Name und weitere Adressen werden
  nicht gespeichert.
- **Client-Secret** verschlüsselt in `auth_github_providers.client_secret` (`EncryptedStr`,
  Rotation mit `python -m app.cli rotate-keys`).
- **Open Redirect:** `return_to` akzeptiert nur Pfade dieser Seite.
- Anfragen an GitHub haben ein Timeout von 10 Sekunden und folgen keinen Redirects; Antworten über
  1 MiB werden verworfen.
