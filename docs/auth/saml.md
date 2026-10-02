# Anmeldung mit SAML 2.0

Für Organisationen, deren Identity-Provider (IdP) nur SAML anbietet oder die SAML bevorzugen:
Microsoft Entra ID, AD FS, Okta, Keycloak und jeder andere SAML-2.0-IdP. ollamail ist dabei der
**Service Provider (SP)**. Beim ersten Login wird das ollamail-Konto automatisch angelegt
(Just-in-Time-Provisioning wie bei OIDC); Gruppen aus der Assertion steuern auf Wunsch die Rolle.

Inhalt:

1. [Überblick](#1-überblick)
2. [Voraussetzungen](#2-voraussetzungen)
3. [Einrichten in ollamail](#3-einrichten-in-ollamail)
4. [Microsoft Entra ID](#4-microsoft-entra-id)
5. [AD FS](#5-ad-fs)
6. [Keycloak](#6-keycloak)
7. [Okta und andere IdPs](#7-okta-und-andere-idps)
8. [Attribute, Kennung und Gruppen](#8-attribute-kennung-und-gruppen)
9. [Admin-API](#9-admin-api)
10. [Prüfungen der SAML-Response](#10-prüfungen-der-saml-response)
11. [Fehlercodes auf der Login-Seite](#11-fehlercodes-auf-der-login-seite)
12. [Sicherheit und Datenschutz](#12-sicherheit-und-datenschutz)
13. [Grenzen](#13-grenzen)

## 1. Überblick

```
Browser ── GET /api/auth/saml/<name>/login ──▶ ollamail
Browser ◀── 303 an den IdP: SAMLRequest (HTTP-Redirect-Binding) + RelayState ── ollamail
Browser ── Anmeldung beim IdP (Passwort, MFA, Kerberos …) ──▶ IdP
Browser ◀── HTML-Formular mit signierter SAMLResponse ── IdP
Browser ── POST /api/auth/saml/acs (SAMLResponse, RelayState) ──▶ ollamail
          ollamail prüft Signatur, Zeitfenster, Audience, Destination, InResponseTo, Replay,
          ordnet die Identität zu (Provisioning, Rollen-Zuordnung), startet die Session
Browser ◀── 303 zurück in die App (Session-Cookie) ── ollamail
```

- **Nur SP-initiiert.** Die Anmeldung beginnt immer in ollamail. Vom IdP gestartete Logins
  (IdP-initiated, z. B. Kachel in „My Apps“) werden abgelehnt (`state_invalid`): Ohne
  vorausgehende Anfrage lässt sich weder Login-CSRF noch Replay sauber ausschließen.
- Der Ablauf nutzt denselben Redirect-Flow wie OIDC und GitHub (`backend/app/auth/redirect_flow.py`):
  verschlüsseltes Einmal-Cookie, Fehler als Redirect auf `/login?error=<code>`, gemeinsames
  Provisioning. Die ID des AuthnRequest wird aus dem `nonce` des Flows abgeleitet, `RelayState`
  ist der `state`.
- Bibliothek: [python3-saml](https://github.com/SAML-Toolkits/python3-saml) (XML-Signatur über
  xmlsec). ollamail implementiert keine eigene Signaturprüfung.
- Code: `backend/app/auth/providers/saml/`.

## 2. Voraussetzungen

- ollamail ist per **HTTPS** erreichbar, und `OLLAMAIL_AUTH_PUBLIC_URL` ist gesetzt (siehe
  [`oidc.md`](oidc.md) §2). Daraus entstehen die Adressen, die beim IdP eingetragen werden:

  | Was | Adresse | Beim IdP heißt das |
  |---|---|---|
  | SP-Metadaten | `https://mail.example.org/api/auth/saml/<name>/metadata` | Metadaten-URL des SP |
  | Entity-ID | standardmäßig dieselbe Adresse wie die SP-Metadaten | Bezeichner, Identifier, Audience, Client-ID (Keycloak) |
  | Assertion Consumer Service (ACS) | `https://mail.example.org/api/auth/saml/acs` | Antwort-URL, Reply URL, ACS URL (Binding HTTP-POST) |

  `<name>` ist der Kurzname des Providers in ollamail. Die ACS-URL ist für alle SAML-Provider
  gleich; welcher Provider gemeint ist, steht im verschlüsselten Flow-Cookie.
- Die Host-Adresse muss einen Domainnamen haben (z. B. `mail.example.org`, `localhost` oder eine
  IP-Adresse); einteilige Intranet-Namen wie `https://ollamail/` lehnt python3-saml als ACS-URL ab.
- Der IdP schickt die Antwort per Cross-Site-POST. Das Flow-Cookie wird deshalb mit
  `SameSite=None; Secure` gesetzt (nur mit `OLLAMAIL_AUTH_COOKIE_SECURE=true`, dem Standard).
  Ohne HTTPS (Entwicklung) müssen IdP und ollamail auf derselben Site liegen, z. B. beide auf
  `localhost`.
- Es gibt **keine neuen Umgebungsvariablen**. SAML-Provider werden über Admin → Anmeldung bzw.
  die Admin-API gepflegt.

## 3. Einrichten in ollamail

Admin → Anmeldung → **Anbieter hinzufügen** → **SAML 2.0**:

1. **Produkt** wählen (Entra ID, AD FS, Okta, Keycloak, anderer). Das Produkt setzt nur die
   Standardwerte für Attributnamen und NameID-Format (§8), an der Prüfung ändert es nichts.
2. **Name auf der Login-Seite** und **Kennung** (`<name>`, Teil der Entity-ID, später nicht
   änderbar).
3. **IdP-Metadaten**: am besten die **Metadaten-URL** des IdP (dann lassen sich Zertifikate nach
   einem Wechsel mit „Metadaten neu laden“ aktualisieren) oder die Metadaten-Datei hochladen.
4. „Speichern und weiter“: Der Provider wird **deaktiviert** gespeichert. Schritt 3 zeigt
   SP-Metadaten-URL/Entity-ID und ACS-URL zum Kopieren. Diese beim IdP eintragen (§4–§7), dann
   „Jetzt aktivieren“.

Danach erscheint auf der Login-Seite ein Button mit dem gewählten Namen. Weitere Einstellungen
(Attributnamen, `trust_email`, Domain-Allowlist, `link_by_email`, eigene Entity-ID) über die
Admin-API (§9).

## 4. Microsoft Entra ID

1. Entra Admin Center → *Enterprise applications* → *New application* → *Create your own
   application* → „Integrate any other application you don't find in the gallery (Non-gallery)“.
2. *Single sign-on* → *SAML* → *Basic SAML Configuration*:
   - *Identifier (Entity ID)*: Entity-ID aus ollamail (Schritt 3 des Assistenten)
   - *Reply URL (Assertion Consumer Service URL)*: ACS-URL aus ollamail
   - *Sign on URL*: `https://mail.example.org/api/auth/saml/<name>/login` (optional; startet die
     Anmeldung SP-initiiert)
3. *Attributes & Claims*: Die Standard-Claims passen zum Preset „Microsoft Entra ID“
   (E-Mail `…/claims/emailaddress`, Anzeigename `…/identity/claims/displayname`). Für Gruppen
   *Add a group claim* → „Security groups“ bzw. „Groups assigned to the application“, Quelle
   „Group ID“. Gemeldet werden dann Objekt-IDs; diese in der Rollen-Zuordnung eintragen.
4. *SAML Certificates* → *App Federation Metadata Url* kopieren und in ollamail als
   Metadaten-URL eintragen. Entra signiert standardmäßig die Assertion mit SHA-256.
5. *Users and groups*: Wer sich anmelden darf, wird hier zugewiesen (bei „Assignment required“).

**Kennung:** Das Preset nutzt das Attribut
`http://schemas.microsoft.com/identity/claims/objectidentifier` (Objekt-ID des Nutzers), nicht die
NameID: Deren Standard ist der UPN, der sich bei Umbenennungen ändert.

## 5. AD FS

1. AD FS-Verwaltung → *Relying Party Trusts* → *Add Relying Party Trust* → *Claims aware* →
   „Import data about the relying party published online“ mit der **SP-Metadaten-URL** von
   ollamail (oder die Metadaten-Datei herunterladen und importieren). AD FS übernimmt Entity-ID
   und ACS-URL.
2. *Access control policy* nach Bedarf (z. B. „Permit everyone“ oder eine Gruppe).
3. *Edit Claim Issuance Policy*:
   - „Send LDAP Attributes as Claims“, Attribute Store *Active Directory*:
     `E-Mail-Addresses` → *E-Mail Address*, `Display-Name` → *Name*,
     `Token-Groups - Unqualified Names` → *Group*.
   - „Transform an Incoming Claim“: *E-Mail Address* (oder *UPN*) → *Name ID*, Format
     *Persistent Identifier*. Besser stabil: eine eigene Regel, die `objectGUID` als persistente
     NameID ausgibt.
4. In ollamail das Preset „AD FS“ wählen und als Metadaten-URL
   `https://adfs.example.org/FederationMetadata/2007-06/FederationMetadata.xml` eintragen.

AD FS signiert die Assertion standardmäßig mit SHA-256. Falls der Trust auf SHA-1 steht
(*Properties* → *Advanced* → *Secure hash algorithm*), auf SHA-256 umstellen: SHA-1 lehnt
ollamail ab.

## 6. Keycloak

1. Realm → *Clients* → *Create client*, *Client type* `SAML`, *Client ID* = Entity-ID aus ollamail.
2. *Valid redirect URIs* und *Assertion Consumer Service POST Binding URL* (Tab *Advanced*):
   ACS-URL aus ollamail.
3. Tab *Settings*: *Name ID format* `persistent`, *Force name ID format* an; *Sign documents*
   an (Standard), optional *Sign assertions*. *Signature algorithm* `RSA_SHA256`.
   Tab *Keys*: *Client signature required* **aus** (ollamail signiert seine Anfragen nicht).
4. Tab *Client scopes* → `<client>-dedicated` → *Add mapper* → *By configuration*:
   - *User Property* `email` → SAML-Attribut `email`
   - *User Property* `firstName` (oder ein Attribut mit dem vollen Namen) → `displayName`
   - *Group list* → `groups`, *Full group path* aus
5. In ollamail das Preset „Keycloak“ und als Metadaten-URL
   `https://sso.example.org/realms/<realm>/protocol/saml/descriptor` eintragen.

Die persistente NameID von Keycloak (`G-<uuid>`) ist je Nutzer und Client stabil.

## 7. Okta und andere IdPs

**Okta:** *Applications* → *Create App Integration* → *SAML 2.0*. *Single sign-on URL* = ACS-URL
(„Use this for Recipient URL and Destination URL“ an), *Audience URI* = Entity-ID, *Name ID
format* `Persistent`. *Attribute Statements*: `email` → `user.email`, `displayName` →
`user.displayName`; *Group Attribute Statements*: `groups` mit passendem Filter. Die *Metadata URL*
(Tab *Sign On*) in ollamail eintragen.

**Andere IdPs** brauchen:

- SP-initiiertes SSO mit HTTP-Redirect-Binding für die Anfrage und HTTP-POST für die Antwort,
- eine RSA-SHA-256-Signatur (oder stärker) auf Response oder Assertion,
- eine stabile Kennung (persistente NameID oder ein Attribut, §8), `Destination`, `Recipient`,
  `InResponseTo` und `AudienceRestriction` in der Antwort (Standard bei allen gängigen IdPs).

## 8. Attribute, Kennung und Gruppen

| Einstellung | Bedeutung | Standard (Preset „generic“, „keycloak“, „okta“) |
|---|---|---|
| `name_id_format` | NameID-Format, das ollamail anfragt | `persistent` (Entra: `unspecified`) |
| `subject_attribute` | Attribut mit der stabilen Nutzerkennung; `null` = NameID | `null` (Entra: Objekt-ID) |
| `email_attribute` | Attribut mit der E-Mail-Adresse | `email` |
| `display_name_attribute` | Attribut mit dem Anzeigenamen | `displayName` |
| `groups_attribute` | Attribut mit den Gruppen (mehrwertig); `null` = keine Gruppen | `groups` |
| `trust_email` | Der IdP bürgt für die Adresse (Verzeichnis-Attribut) | `false` |

- **Kennung (`subject`)**: NameID oder `subject_attribute`, max. 255 Zeichen. Eine **transiente**
  NameID wird abgelehnt (`transient_name_id`): Sie wechselt bei jedem Login und würde jedes Mal
  ein neues Konto anlegen wollen. Die Identität ist `saml:<name>` + Kennung.
- **E-Mail**: aus `email_attribute`; fehlt es und hat die NameID das Format `emailAddress`, die
  NameID. Ohne Adresse kann ein neuer Nutzer nicht angelegt werden (`email_missing`).
- **`trust_email`**: Nur mit `true` gilt die Adresse als verifiziert. Erst dann wirken
  `allowed_domains` (sonst wird jede Anmeldung mit `domain_not_allowed` abgelehnt) und
  `link_by_email`. Nur einschalten, wenn Nutzer ihre Adresse beim IdP nicht selbst ändern können.
- **Gruppen** werden bei jedem Login in `auth_identities.groups` aktualisiert und von der
  [Rollen-Zuordnung](admin.md#3-rollen-zuordnung-gruppen--rollen) ausgewertet (Provider
  `saml:<name>`). Entra meldet Objekt-IDs, AD FS Gruppennamen, Keycloak/Okta Gruppennamen.

## 9. Admin-API

Alle Endpunkte erfordern die Rolle `admin`. Jede Änderung landet im Audit-Log
(`idp.config_changed`, `kind: "saml"`, `change`: `created`, `updated`, `metadata_refreshed`,
`deleted`; keine Werte).

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/admin/auth/saml/providers` | Alle SAML-Provider |
| `POST` | `/api/admin/auth/saml/providers` | Provider anlegen |
| `GET` | `/api/admin/auth/saml/providers/{name}` | Ein Provider |
| `PATCH` | `/api/admin/auth/saml/providers/{name}` | Ändern; fehlende Felder bleiben |
| `POST` | `/api/admin/auth/saml/providers/{name}/refresh-metadata` | IdP-Metadaten erneut von `metadata_url` laden |
| `DELETE` | `/api/admin/auth/saml/providers/{name}` | Löschen |
| `GET` | `/api/auth/saml/{name}/metadata` | SP-Metadaten (öffentlich, auch für deaktivierte Provider) |

Anlegen mit Metadaten-URL:

```bash
curl -X POST https://mail.example.org/api/admin/auth/saml/providers \
  -H 'Content-Type: application/json' -H "X-CSRF-Token: $CSRF" -b "$COOKIES" \
  -d '{
    "name": "adfs",
    "display_name": "Firmenkonto",
    "preset": "adfs",
    "metadata_url": "https://adfs.example.org/FederationMetadata/2007-06/FederationMetadata.xml",
    "enabled": false
  }'
```

Die IdP-Daten kommen aus genau einer Quelle: `metadata_xml` (Upload, wird nicht gespeichert),
`metadata_url` (sofort geladen) oder `idp_entity_id` + `idp_sso_url` + `idp_certificates` (PEM
oder Base64). Weitere Felder: `name`, `display_name`, `preset`, `sp_entity_id` (`null` = SP-Metadaten-URL),
die Attributfelder aus §8, `enabled`, `auto_provision`, `link_by_email`, `allowed_domains`.
Fehlen Attributfelder beim Anlegen, setzt das Preset sie. Antworten enthalten zusätzlich
`provider` (`saml:<name>`), `effective_sp_entity_id`, `redirect_uri` (ACS-URL),
`sp_metadata_url` und je IdP-Zertifikat Fingerprint (SHA-256) und Ablaufdatum.

Ungültige Metadaten (kein IdP, keine SSO-Adresse mit HTTP-Redirect-Binding, kein
Signaturzertifikat, DTD/Entities, über 2 MiB) oder eine nicht erreichbare Metadaten-URL ergeben
422 (`saml-metadata-invalid`) mit einer festen Meldung ohne Inhalte des Dokuments.

**Zertifikatswechsel beim IdP:** Neues Zertifikat beim IdP hinzufügen, in ollamail
„Metadaten neu laden“ (oder beide Zertifikate über `idp_certificates` eintragen), dann beim IdP
umschalten. ollamail akzeptiert jedes eingetragene Signaturzertifikat. Die Detailansicht zeigt das
Ablaufdatum.

**Löschen/Deaktivieren** lässt Nutzer und ihre Identitäten bestehen; laufende Sessions bleiben
bis zu ihrem Ablauf gültig. Wie bei allen Anbietern verhindert die Selbstaussperr-Prüfung
([`admin.md`](admin.md) §2) Änderungen, nach denen sich kein Admin mehr anmelden könnte.

## 10. Prüfungen der SAML-Response

python3-saml im *strict mode* prüft:

- XML-Schema, **keine DTD und keine Entity-Deklarationen** (kein XXE, keine Entity-Expansion),
  genau eine Assertion, kein `EncryptedAttribute`
- **XML-Signatur** von Response oder Assertion mit einem eingetragenen IdP-Zertifikat, inklusive
  Schutz gegen Signature Wrapping; ohne Signatur keine Anmeldung; **SHA-1 wird abgelehnt**
- Status `Success`, Issuer = Entity-ID des IdP
- **Zeitfenster**: `NotBefore`/`NotOnOrAfter` der Conditions und der SubjectConfirmation
  (Toleranz für Uhrabweichung: 5 Minuten), `SessionNotOnOrAfter`

ollamail prüft zusätzlich strenger als die Bibliothek:

- **`InResponseTo`** der Response muss die ID des AuthnRequest dieses Logins sein (aus dem
  Flow-Cookie); Antworten ohne `InResponseTo` (unaufgefordert) werden abgelehnt.
- **`Destination`** muss vorhanden und exakt die ACS-URL sein.
- **Audience**: Die Assertion muss eine `AudienceRestriction` mit der Entity-ID von ollamail haben.
- **SubjectConfirmation** (in der signierten Assertion) mit Methode `bearer`, `InResponseTo` dieses
  Logins, `Recipient` = ACS-URL und `NotOnOrAfter`. So ist die Bindung auch dann signiert, wenn
  der IdP nur die Assertion und nicht die ganze Response signiert.
- **Replay-Schutz**: Jede Assertion-ID wird nur einmal angenommen. Benutzte IDs (nur SHA-256 aus
  Provider und ID) stehen in `auth_saml_assertions`, bis die Assertion ohnehin nicht mehr gültig
  wäre (mindestens die Lebensdauer des Flow-Cookies, höchstens 24 Stunden); alle API-Instanzen
  teilen sie.
- `RelayState` muss zum Flow-Cookie passen (wie `state` bei OIDC); der ACS-Endpunkt ist von der
  CSRF-Prüfung ausgenommen, weil der IdP cross-site postet. Diese drei Bindungen (Cookie,
  `InResponseTo`, Signatur) ersetzen dort den CSRF-Token.

## 11. Fehlercodes auf der Login-Seite

Bei Fehlern leitet ollamail auf `/login?error=<code>` um. Zusätzlich zu den gemeinsamen Codes aus
[`oidc.md`](oidc.md) §12 (`provider_unknown`, `too_many_attempts`, `state_invalid`, `email_missing`,
`domain_not_allowed`, `email_conflict`, `not_provisioned`, `inactive`):

| Code | Bedeutung |
|---|---|
| `idp_error` | Der IdP hat die Anmeldung mit einem Fehlerstatus beendet (z. B. abgebrochen, kein Zugriff) |
| `invalid_response` | Die SAML-Response hat eine Prüfung aus §10 nicht bestanden |
| `provider_unavailable` | Die Provider-Einstellungen sind unbrauchbar (z. B. ungültiges Zertifikat) |

Das Log (`login_failed`) enthält Provider, Code und die fehlgeschlagene Prüfung als statischen
Wert (`check`, z. B. `invalid_signature`, `assertion_expired`, `wrong_audience`,
`wrong_destination`, `wrong_inresponseto`, `wrong_subjectconfirmation`, `replay`,
`transient_name_id`, `malformed`). Meldungstexte der Bibliothek, Attributwerte, NameID oder die
Response selbst werden nie geloggt. Das Audit-Log vermerkt `auth.login_failed` mit Provider und
Code.

## 12. Sicherheit und Datenschutz

- **Gespeichert** werden je Provider nur Konfiguration und öffentliche IdP-Zertifikate
  (`auth_saml_providers`, keine Secrets). Je Nutzer: Kennung (NameID bzw. Attribut), Gruppen (für
  das Rollen-Mapping) und beim ersten Login E-Mail-Adresse und Anzeigename. Weitere Attribute
  der Assertion werden verworfen. Die E-Mail-Adresse in ollamail wird bei späteren Logins nicht
  überschrieben.
- **Replay-Cache** (`auth_saml_assertions`): nur SHA-256-Werte und Ablaufzeit, keine
  personenbezogenen Daten; abgelaufene Einträge werden beim nächsten Login gelöscht.
- **Metadaten** werden nur über HTTPS geladen (`http` nur für `localhost`), mit
  Zertifikatsprüfung, ohne Redirects, mit 10 Sekunden Timeout und höchstens 2 MiB.
- **Open Redirect:** `return_to` akzeptiert nur Pfade dieser Seite.
- ollamail signiert keine Anfragen und hat keinen privaten SAML-Schlüssel; es gibt nichts zu
  rotieren. IdPs, die signierte AuthnRequests verlangen, werden nicht unterstützt (§13).

## 13. Grenzen

- **Kein Single Logout (SLO).** Abmelden beendet die ollamail-Session, nicht die Sitzung beim
  IdP. Wer sich anschließend erneut anmeldet, wird vom IdP ggf. ohne Passwort durchgelassen.
- **Keine verschlüsselten Assertions** und **keine signierten AuthnRequests** (ollamail hat keinen
  SP-Schlüssel). TLS schützt den Transport; die Assertion ist signiert.
- **Kein IdP-initiiertes SSO** (siehe §1).
- **Keine Artifact-Binding**, nur HTTP-Redirect (Anfrage) und HTTP-POST (Antwort).
