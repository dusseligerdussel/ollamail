# Anmeldung mit OpenID Connect (OIDC)

Mitarbeitende melden sich mit ihrem Firmenkonto an (Microsoft Entra ID, Google Workspace,
Keycloak, Authentik oder ein anderer OIDC-Provider). Beim ersten Login wird das ollamail-Konto
automatisch angelegt (Just-in-Time-Provisioning). Mehrere Provider können gleichzeitig aktiv sein.

Inhalt:

1. [Überblick](#1-überblick)
2. [Voraussetzungen](#2-voraussetzungen)
3. [Microsoft Entra ID](#microsoft-entra-id)
4. [Google Workspace](#google-workspace)
5. [Keycloak](#keycloak)
6. [Authentik](#authentik)
7. [Generischer Provider](#generic)
8. [Konfiguration per Umgebung (GitOps)](#8-konfiguration-per-umgebung-gitops)
9. [Admin-API](#9-admin-api)
10. [Kontoanlage, Verknüpfung und Gruppen](#10-kontoanlage-verknüpfung-und-gruppen)
11. [Abmelden](#11-abmelden)
12. [Fehlercodes auf der Login-Seite](#12-fehlercodes-auf-der-login-seite)
13. [Sicherheit](#13-sicherheit)

## 1. Überblick

```
Browser ── GET /api/auth/oidc/<name>/login ──▶ ollamail ── 303 ──▶ IdP (Login, MFA, Consent)
Browser ◀────────────────────────── 302 ─────────────────────────── IdP
Browser ── GET /api/auth/oidc/<name>/callback?code&state ──▶ ollamail
                                   ollamail ── Code + PKCE-Verifier ──▶ IdP (Token-Endpunkt)
                                   ollamail prüft ID-Token, legt Konto an, startet Session
Browser ◀── 303 zurück in die App (Session-Cookie) ── ollamail
```

- Authorization Code Flow mit **PKCE (S256)**, `state` und `nonce`.
- Das ID-Token wird vollständig geprüft (Signatur, Issuer, Audience, Ablauf, Nonce, bei Entra der
  Tenant, bei Google die Workspace-Domain). Danach gilt die normale serverseitige Session von
  ollamail; Tokens des IdP werden nicht gespeichert.
- Die Login-Seite fragt `GET /api/auth/providers` ab. Jeder aktive Provider erscheint dort mit
  `login_path` (z. B. `/auth/oidc/entra/login`); der Login-Button navigiert zu
  `/api` + `login_path`, optional mit `?return_to=/pfad`.

## 2. Voraussetzungen

- ollamail ist per **HTTPS** erreichbar (Reverse Proxy, siehe `docs/OPERATIONS.md` §4).
- `OLLAMAIL_AUTH_PUBLIC_URL` ist gesetzt, z. B. `https://mail.example.org` (ohne `/` am Ende).
  Daraus entsteht die **Redirect-URI**, die beim IdP registriert wird:

  ```
  https://mail.example.org/api/auth/oidc/<name>/callback
  ```

  `<name>` ist der Kurzname des Providers in ollamail (z. B. `entra`, `google`). Die Admin-API
  zeigt die fertige Redirect-URI im Feld `redirect_uri` an. Ohne `OLLAMAIL_AUTH_PUBLIC_URL` wird sie
  aus der Anfrage abgeleitet (Host, `X-Forwarded-Proto`). Das funktioniert hinter dem mitgelieferten
  Proxy, ist aber fehleranfällig, wenn die Instanz unter mehreren Namen erreichbar ist.
- Der Issuer muss per HTTPS erreichbar sein. Nur für lokale Test-IdPs gibt es
  `OLLAMAIL_AUTH_OIDC_ALLOW_INSECURE_HTTP=true`.

## Microsoft Entra ID

### App registrieren

1. [Entra Admin Center](https://entra.microsoft.com) → **Identität → Anwendungen →
   App-Registrierungen → Neue Registrierung**.
2. Name: `ollamail`. **Unterstützte Kontotypen**:
   - *Nur Konten in diesem Organisationsverzeichnis* (Single Tenant, empfohlen) oder
   - *Konten in einem beliebigen Organisationsverzeichnis* (Multi-Tenant, z. B. für mehrere
     Firmen einer Gruppe).
3. **Umleitungs-URI**: Plattform *Web*, Wert
   `https://mail.example.org/api/auth/oidc/entra/callback`.
4. Nach dem Anlegen notieren: **Anwendungs-ID (Client-ID)** und **Verzeichnis-ID (Tenant-ID)**.
5. **Zertifikate & Geheimnisse → Neuer geheimer Clientschlüssel**. Den *Wert* (nicht die ID)
   notieren; Ablaufdatum im Kalender vormerken.
6. **API-Berechtigungen**: `openid`, `email`, `profile` (Microsoft Graph, delegiert) sind
   Standard. Ggf. Administratorzustimmung erteilen.
7. Optional **Tokenkonfiguration**:
   - **Gruppenanspruch hinzufügen** → *Sicherheitsgruppen* (für das Rollen-Mapping). Entra liefert
     Gruppen-**Object-IDs**, keine Namen. Bei mehr als 200 Gruppen lässt Entra den Claim weg
     („Groups Overage“); dann *Gruppen, die der Anwendung zugewiesen sind* wählen.
   - **Optionalen Anspruch hinzufügen** → ID-Token → `email` und `xms_edov`. `xms_edov` sagt, ob die
     Domain der Adresse im Tenant verifiziert ist. Ohne diesen Claim gilt die E-Mail-Adresse aus
     Entra als **nicht verifiziert** (sie ist in vielen Tenants frei änderbar), siehe §10.

### In ollamail eintragen

| Feld | Wert |
|---|---|
| `name` | `entra` |
| `preset` | `entra` |
| `issuer` | Single Tenant: `https://login.microsoftonline.com/<Tenant-ID>/v2.0` · Multi-Tenant: `https://login.microsoftonline.com/organizations/v2.0` |
| `client_id` | Anwendungs-ID |
| `client_secret` | Wert des geheimen Clientschlüssels |
| `allowed_tenants` | Nur Multi-Tenant, **Pflicht**: Liste der erlaubten Tenant-IDs |
| `groups_claim` | `groups` |

Prüfungen des Presets:

- **Single Tenant:** Der Claim `tid` muss der Tenant-ID im Issuer entsprechen.
- **Multi-Tenant:** `tid` muss in `allowed_tenants` stehen, und der Issuer des Tokens muss
  `https://login.microsoftonline.com/<tid>/v2.0` lauten. Ohne `allowed_tenants` wird die
  Konfiguration abgelehnt, denn sonst könnte sich jeder Entra-Tenant der Welt anmelden.
- Der Tenant muss als GUID angegeben werden, nicht als Domain (`contoso.onmicrosoft.com`).
  `consumers` (private Microsoft-Konten) wird nicht unterstützt.

## Google Workspace

### OAuth-Client anlegen

1. [Google Cloud Console](https://console.cloud.google.com) → Projekt wählen oder anlegen.
2. **APIs & Dienste → OAuth-Zustimmungsbildschirm**: Nutzertyp **Intern** (nur Konten der eigenen
   Workspace-Organisation). Bereiche `openid`, `email`, `profile`.
3. **APIs & Dienste → Anmeldedaten → Anmeldedaten erstellen → OAuth-Client-ID**, Typ
   **Webanwendung**.
4. **Autorisierte Weiterleitungs-URIs**: `https://mail.example.org/api/auth/oidc/google/callback`.
5. Client-ID und Clientschlüssel notieren.

### In ollamail eintragen

| Feld | Wert |
|---|---|
| `name` | `google` |
| `preset` | `google` |
| `issuer` | `https://accounts.google.com` |
| `client_id`, `client_secret` | aus Schritt 5 |
| `hosted_domains` | Workspace-Domain(s), z. B. `["example.org"]` – **dringend empfohlen** |
| `groups_claim` | `null` (Google liefert keine Gruppen im ID-Token) |

Mit `hosted_domains` muss der Claim `hd` des ID-Tokens auf der Liste stehen. Die E-Mail-Domain
allein reicht nicht: private Google-Konten können mit beliebigen Adressen angelegt werden. Ist genau
eine Domain eingetragen, schickt ollamail sie zusätzlich als Hinweis (`hd=`) an die Kontoauswahl.

## Keycloak

1. Realm wählen → **Clients → Create client**: Typ *OpenID Connect*, Client-ID `ollamail`.
2. **Client authentication** an (vertraulicher Client), **Standard flow** an, alle anderen Flows aus.
3. **Valid redirect URIs**: `https://mail.example.org/api/auth/oidc/keycloak/callback`;
   **Valid post logout redirect URIs**: `https://mail.example.org/login`.
4. **Advanced → Proof Key for Code Exchange Code Challenge Method**: `S256`.
5. **Credentials**: Client-Secret kopieren.
6. Gruppen: **Client scopes → ollamail-dedicated → Add mapper → By configuration → Group
   Membership**, Token-Claim-Name `groups`, *Full group path* nach Wunsch.

In ollamail: `preset` `keycloak`, `issuer` `https://sso.example.org/realms/<realm>`.

## Authentik

1. **Applications → Providers → Create → OAuth2/OpenID Provider**: Client-Typ *Confidential*,
   Redirect-URI `https://mail.example.org/api/auth/oidc/authentik/callback`, Signaturschlüssel
   auswählen (RS256; ohne Schlüssel signiert Authentik mit HS256, das ollamail ablehnt).
2. **Applications → Create**: Slug z. B. `ollamail`, Provider aus Schritt 1.
3. Die Scopes `openid`, `email`, `profile` sind Standard; `profile` enthält bei Authentik den Claim
   `groups`.

In ollamail: `preset` `authentik`, `issuer` `https://auth.example.org/application/o/<slug>/`
(mit `/` am Ende, genau wie im Discovery-Dokument).

<a id="generic"></a>
## Generischer Provider

Jeder OIDC-Provider mit Discovery (`<issuer>/.well-known/openid-configuration`), Authorization Code
Flow und asymmetrisch signierten ID-Tokens (RS*, PS*, ES*, EdDSA) funktioniert. `preset` `generic`,
`issuer` genau so, wie er im Discovery-Dokument steht. Öffentliche Clients ohne Secret sind möglich
(nur PKCE); `client_secret` dann weglassen.

## 8. Konfiguration per Umgebung (GitOps)

`OLLAMAIL_AUTH_OIDC_PROVIDERS` enthält ein JSON-Objekt `{"<name>": {...}}` mit denselben Feldern wie
die Admin-API. Solche Provider sind in der Admin-API sichtbar (`source: "env"`), aber nicht
änderbar. Ungültige Einträge verhindern den Start der API (Fehlermeldung nennt den Eintrag).

```env
OLLAMAIL_AUTH_PUBLIC_URL=https://mail.example.org
OLLAMAIL_AUTH_OIDC_PROVIDERS={"entra": {"display_name": "Microsoft", "preset": "entra", "issuer": "https://login.microsoftonline.com/<tenant-id>/v2.0", "client_id": "<app-id>", "client_secret": "<secret>", "groups_claim": "groups"}}
```

Das Secret steht hier im Klartext in der Umgebung; für Kubernetes/Compose ein Secret-Objekt bzw.
eine nicht eingecheckte `.env` verwenden.

| Einstellung | Standard | Bedeutung |
|---|---|---|
| `OLLAMAIL_AUTH_PUBLIC_URL` | leer | Öffentliche URL der Web-Oberfläche, Basis der Redirect-URIs |
| `OLLAMAIL_AUTH_OIDC_PROVIDERS` | `{}` | Provider aus der Umgebung |
| `OLLAMAIL_AUTH_OIDC_ALLOW_INSECURE_HTTP` | `false` | `http://`-Issuer erlauben (nur Test-IdPs) |
| `OLLAMAIL_AUTH_OIDC_METADATA_CACHE_SECONDS` | `3600` | Cache für Discovery-Dokument und Signaturschlüssel |

## 9. Admin-API

Alle Endpunkte erfordern die Rolle `admin`. Die Oberfläche dafür ist Admin → Anmeldung
([`admin.md`](admin.md)). Deaktivieren, Ändern und Löschen werden mit 409 (`admin-lockout`)
abgelehnt, wenn sich danach kein Admin mehr anmelden könnte.

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/admin/auth/oidc/presets` | Presets mit Vorgaben (Issuer-Vorlage, Scopes, Gruppen-Claim, zusätzliche Felder) |
| `GET` | `/api/admin/auth/oidc/providers` | Alle Provider (Umgebung und Datenbank) |
| `POST` | `/api/admin/auth/oidc/providers` | Provider anlegen |
| `GET` | `/api/admin/auth/oidc/providers/{name}` | Ein Provider |
| `PATCH` | `/api/admin/auth/oidc/providers/{name}` | Ändern; fehlende Felder bleiben, `client_secret: null` löscht das Secret |
| `DELETE` | `/api/admin/auth/oidc/providers/{name}` | Löschen |

Felder:

| Feld | Standard | Bedeutung |
|---|---|---|
| `name` | – | Kurzname, `a-z`, `0-9`, `-`, max. 32 Zeichen; Teil der Redirect-URI, nicht änderbar |
| `display_name` | – | Text des Login-Buttons |
| `preset` | `generic` | `generic`, `entra`, `google`, `keycloak`, `authentik` |
| `issuer` | – | Issuer-URL (HTTPS) |
| `client_id` / `client_secret` | – | Client-Zugangsdaten; das Secret wird verschlüsselt gespeichert und nie ausgegeben (`has_client_secret`) |
| `scopes` | `openid email profile` | muss `openid` enthalten |
| `enabled` | `true` | Provider auf der Login-Seite anbieten |
| `auto_provision` | `true` | Unbekannte Nutzer beim ersten Login anlegen (Rolle `user`) |
| `link_by_email` | `false` | Vorhandenen Nutzer mit gleicher, **verifizierter** E-Mail-Adresse verknüpfen |
| `allowed_domains` | `[]` | Erlaubte E-Mail-Domains (leer: alle) |
| `groups_claim` | `groups` | Claim mit den Gruppen; `null` für keine |
| `allowed_tenants` | `[]` | Nur Entra: erlaubte Tenant-IDs |
| `hosted_domains` | `[]` | Nur Google: erlaubte Workspace-Domains (`hd`) |

Löschen oder Deaktivieren eines Providers lässt Nutzer und ihre verknüpften Identitäten bestehen;
wird er unter demselben Namen mit demselben IdP neu angelegt, funktionieren die Logins wieder.
Laufende Sessions bleiben bis zu ihrem Ablauf gültig; zum sofortigen Aussperren den Nutzer
deaktivieren.

## 10. Kontoanlage, Verknüpfung und Gruppen

Umgesetzt in `backend/app/auth/provisioning.py` (gemeinsam für OIDC, GitHub #31 und LDAP #32):

1. **Bekannte Identität** (Provider + `sub`): Login als zugehöriger Nutzer. Die Gruppen werden bei
   jedem Login aktualisiert. Die E-Mail-Adresse in ollamail wird nicht überschrieben.
2. **Unbekannte Identität, Adresse schon vergeben:** Verknüpfung nur, wenn `link_by_email` aktiv ist
   **und** der IdP die Adresse als verifiziert meldet (`email_verified: true`, bei Entra
   `xms_edov: true`). Sonst wird der Login abgelehnt (`email_conflict`). Eine Verknüpfung über eine
   unverifizierte Adresse würde Kontoübernahmen erlauben.
3. **Unbekannte Identität, neue Adresse:** Anlage mit Rolle `user`, wenn `auto_provision` aktiv ist.
   Ohne E-Mail-Adresse (auch nicht über den Userinfo-Endpunkt) kein Login (`email_missing`).

Die **Domain-Allowlist** gilt bei jedem Login, auch für bekannte Identitäten, und akzeptiert nur
verifizierte Adressen. Für Entra ohne `xms_edov` deshalb die Allowlist leer lassen; der Tenant
(`tid`) ist dort die Grenze.

**Gruppen** stehen in `auth_identities.groups` (je Identität, maximal 500, Stand des letzten
Logins). Die Rollen-Zuordnung ([`admin.md`](admin.md#3-rollen-zuordnung-gruppen--rollen)) wertet
sie bei jedem Login aus; bei Entra ID sind es die Objekt-IDs der Gruppen.

## 11. Abmelden

`POST /api/auth/oidc/logout` beendet die ollamail-Session (wie `POST /api/auth/logout`) und liefert
`{"redirect_url": ...}`. Wurde die Session über einen Provider mit `end_session_endpoint` gestartet
(RP-initiated Logout), navigiert die Oberfläche dorthin; der IdP leitet danach auf
`<public url>/login` zurück. Sonst ist `redirect_url` `null`. Diese Adresse beim IdP als erlaubte
Post-Logout-Redirect-URI eintragen (Keycloak: *Valid post logout redirect URIs*; Entra: als weitere
Umleitungs-URI der Plattform *Web*; Authentik: als weitere Redirect-URI).

## 12. Fehlercodes auf der Login-Seite

Bei Fehlern leitet ollamail auf `/login?error=<code>` um. Die Codes sind statisch und enthalten keine
Nutzerdaten:

| Code | Bedeutung |
|---|---|
| `provider_unknown` | Provider existiert nicht oder ist deaktiviert |
| `provider_unavailable` | Discovery-Dokument oder Schlüssel nicht abrufbar oder ungültig (falscher Issuer, kein HTTPS) |
| `too_many_attempts` | Zu viele Login-Versuche von dieser IP |
| `state_invalid` | Login abgelaufen (10 Minuten), in einem anderen Tab neu gestartet oder manipuliert |
| `idp_error` | Der IdP hat den Login abgebrochen (z. B. Zustimmung verweigert) |
| `token_exchange_failed` | Code-Einlösung fehlgeschlagen (Client-Secret, Redirect-URI, PKCE) |
| `invalid_token` | ID-Token ungültig (Signatur, Issuer, Audience, Ablauf, Nonce, Tenant, Domain) |
| `email_missing` | Der IdP liefert keine E-Mail-Adresse |
| `domain_not_allowed` | Domain nicht erlaubt oder Adresse nicht verifiziert |
| `email_conflict` | Adresse gehört einem anderen Konto, Verknüpfung nicht erlaubt (oder von der Person selbst gesperrt, #216) |
| `not_provisioned` | Unbekannter Nutzer, Just-in-Time-Provisioning ist aus |
| `inactive` | Konto deaktiviert |

Das Log (`login_failed`) enthält Provider, Code und die fehlgeschlagene Prüfung (z. B.
`check=claim_aud`), nie Claims, Tokens oder E-Mail-Adressen.

## 13. Sicherheit

- **PKCE, `state`, `nonce`:** zufällig je Login, in einem verschlüsselten, signierten Cookie
  (`ollamail_login_flow`, AES-256-GCM, `HttpOnly`, `Secure`, `SameSite=Lax`, 10 Minuten, einmalig).
  Nichts wird serverseitig gespeichert, jede API-Instanz kann den Callback bearbeiten.
- **ID-Token:** Prüfung mit `joserfc` (keine Eigenbau-JWT-Validierung). Nur asymmetrische
  Algorithmen; `none` und HMAC werden abgelehnt. Unbekannte Schlüssel-IDs lösen höchstens einmal
  pro Minute ein Nachladen der JWKS aus (Schlüsselrotation ohne Neustart).
- **Discovery:** Der `issuer` im Discovery-Dokument muss exakt dem konfigurierten Issuer
  entsprechen; alle Endpunkte müssen HTTPS verwenden.
- **Userinfo** wird nur abgefragt, wenn E-Mail oder Gruppen im ID-Token fehlen; der `sub` der
  Antwort muss dem ID-Token entsprechen.
- **Open Redirect:** `return_to` akzeptiert nur Pfade dieser Seite (`/…`, nicht `//…`).
- **Client-Secrets** aus der Admin-API liegen verschlüsselt in `auth_oidc_providers.client_secret`
  (`EncryptedStr`, Rotation mit `python -m app.cli rotate-keys`).
