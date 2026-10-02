# SCIM 2.0 Provisioning (Entra ID, Okta)

Mit SCIM legt der Identity Provider (IdP) Nutzer in ollamail an, hält Name und Adresse aktuell,
deaktiviert und löscht sie und überträgt Gruppen. Wer im Unternehmen ausscheidet, verliert den
Zugriff in dem Moment, in dem der IdP die Deaktivierung sendet – nicht erst beim nächsten Login.

Code: `backend/app/scim/` (Endpunkte `router.py`, Logik `service.py`, Tokens `tokens.py`);
Oberfläche: `frontend/src/routes/admin_.scim.tsx`. Standards: RFC 7643 (Schema) und RFC 7644
(Protokoll), in dem Umfang, den Entra ID und Okta nutzen.

## 1. Einrichten in ollamail

Admin → **SCIM-Provisionierung**:

1. **Token erstellen** (Name, z. B. „Entra ID“, optional mit Ablaufdatum). Der Token
   (`olm_scim_…`) wird genau einmal angezeigt; gespeichert wird nur sein SHA-256 und ein kurzer
   Präfix zur Unterscheidung. Pro IdP einen eigenen Token anlegen; widerrufen wirkt sofort.
2. **Endpunkt-URL** kopieren: `<öffentliche URL>/api/scim/v2` (aus `OLLAMAIL_AUTH_PUBLIC_URL`,
   sonst aus der Anfrage abgeleitet).
3. **SCIM einschalten.** Solange SCIM aus ist, beantwortet ollamail jede SCIM-Anfrage mit 403.
4. Optional unter **Anmeldung provisionierter Nutzer** die Anmeldeanbieter auswählen (z. B. den
   Entra-ID-OIDC-Provider), deren erste Anmeldung über die *bestätigte* E-Mail-Adresse mit dem
   provisionierten Konto verknüpft werden darf (siehe Abschnitt 5).

Die Einstellungen lassen sich auch per API setzen: `GET/PATCH /api/admin/scim`,
`POST /api/admin/scim/tokens`, `DELETE /api/admin/scim/tokens/{id}` (nur Admins).

## 2. Microsoft Entra ID

Entra Admin Center → *Unternehmensanwendungen* → *Neue Anwendung* → *Eigene Anwendung erstellen*
(„Nicht-Katalog-Anwendung integrieren“) → *Bereitstellung*:

| Feld | Wert |
|---|---|
| Bereitstellungsmodus | Automatisch |
| Mandanten-URL | Endpunkt-URL aus ollamail, z. B. `https://mail.example.org/api/scim/v2` |
| Geheimes Token | Token aus ollamail |

„Verbindung testen“, dann unter *Zuordnungen* prüfen:

- **Benutzer:** `userPrincipalName → userName` (Abgleich), `mail → emails[type eq "work"].value`,
  `displayName → displayName`, `Switch([IsSoftDeleted], …) → active`, `objectId → externalId`.
  Weitere Attribute (Telefon, Abteilung, Manager …) dürfen bleiben; ollamail speichert sie nicht.
- **Gruppen:** `displayName → displayName` (Abgleich), `objectId → externalId`, `members → members`.

Bereich: „Nur zugewiesene Benutzer und Gruppen synchronisieren“ und die Nutzer bzw. Gruppen der
Anwendung zuweisen. Dann *Bereitstellung starten*.

Was Entra ID sendet und ollamail daraus macht:

| Ereignis in Entra ID | Anfrage | Wirkung |
|---|---|---|
| Nutzer zugewiesen | `GET /Users?filter=userName eq "…"`, dann `POST /Users` | Konto angelegt (bzw. übernommen, Abschnitt 4) |
| Attribut geändert | `PATCH /Users/{id}` mit `Replace` auf `displayName`, `emails[type eq "work"].value`, `userName` … | Name/Adresse aktualisiert |
| Zuweisung entfernt, Konto deaktiviert oder gelöscht (Papierkorb) | `PATCH /Users/{id}` `active=false` | Konto deaktiviert, **alle Sessions sofort beendet** |
| Endgültig gelöscht (nach 30 Tagen) | `DELETE /Users/{id}` | Nutzer mit allen Daten gelöscht (Abschnitt 6) |
| Gruppe zugewiesen | `GET /Groups?filter=displayName eq "…"&excludedAttributes=members`, `POST /Groups` | Gruppe angelegt |
| Mitglieder geändert | `PATCH /Groups/{id}` `Add`/`Remove` auf `members` (auch `members[value eq "…"]`) | Gruppen der Nutzer aktualisiert |

Ältere Entra-ID-Mandanten senden Booleans als Zeichenkette (`"False"`) – das wird akzeptiert.

## 3. Okta

Okta Admin Console → *Applications* → *Create App Integration* → *SAML 2.0* oder *OIDC*
(für die Anmeldung) → Reiter *General* → *Provisioning: SCIM* → Reiter *Provisioning*:

| Feld | Wert |
|---|---|
| SCIM connector base URL | Endpunkt-URL aus ollamail |
| Unique identifier field for users | `userName` |
| Supported provisioning actions | Push New Users, Push Profile Updates, Push Groups |
| Authentication Mode | HTTP Header (Bearer-Token aus ollamail) |

Unter *To App* „Create Users“, „Update User Attributes“ und „Deactivate Users“ aktivieren;
Gruppen über *Push Groups*. Okta aktualisiert Nutzer mit `PUT /Users/{id}`, deaktiviert mit
`PATCH` ohne `path` (`{"op":"replace","value":{"active":false}}`) und entfernt Mitglieder mit
`members[value eq "…"]`. Okta löscht Nutzer nie per SCIM; deaktivierte Nutzer bleiben in
ollamail deaktiviert, bis ein Admin sie löscht.

## 4. Nutzer

- **Anlegen:** `userName` ist Pflicht und eindeutig (ohne Groß-/Kleinschreibung). Die E-Mail-Adresse
  kommt aus `emails` (primär, sonst `work`, sonst die erste), sonst aus `userName`, wenn er eine
  Adresse ist. Anzeigename aus `displayName`, sonst `name.formatted`, sonst Vor- und Nachname.
  Rolle: Standardrolle des Rollen-Mappings, wenn es aktiv ist, sonst `user`.
- **Bestehende Konten:** Gibt es schon ein Konto mit derselben Adresse, das SCIM noch nicht
  verwaltet (z. B. aus einem früheren Login oder lokal angelegt), übernimmt SCIM dieses Konto,
  statt ein zweites anzulegen. Ein zweiter SCIM-Nutzer mit derselben Adresse oder demselben
  `userName` ergibt 409 (`uniqueness`).
- **Gespeichert** werden nur `userName`, `externalId`, Anzeigename, eine E-Mail-Adresse,
  `active` und Gruppenmitgliedschaften. Alle anderen Attribute (Telefon, Adresse, Abteilung,
  Manager, Passwort …) werden angenommen und verworfen (Datenminimierung). Teil-Änderungen an
  `name.givenName`/`familyName` per `PATCH` ändern den Anzeigenamen nicht – dafür `displayName`
  zuordnen (Standard bei Entra ID und Okta).
- **Deaktivieren** (`active=false`): `is_active` aus, alle Sessions des Nutzers werden in derselben
  Transaktion gelöscht. Jede Anfrage prüft `is_active` (`resolve_session`), der Podcast-Feed
  ebenso; neue Logins lehnt `provision_user` ab. **Reaktivieren** (`active=true`) erlaubt neue
  Logins, alte Sessions bleiben beendet.
- Die SCIM-Liste (`GET /Users`) enthält nur Nutzer, die SCIM angelegt oder übernommen hat.
  Lokale Konten und Konten anderer Anbieter sind für den IdP unsichtbar.

## 5. Anmeldung provisionierter Nutzer

SCIM legt Konten an, angemeldet wird über einen der Anmeldeanbieter (OIDC, GitHub, LDAP,
später SAML). Beim ersten Login gehört die Identität des Anbieters noch zu keinem Konto. Ohne
weitere Einstellung lehnt ollamail die Verknüpfung über die E-Mail-Adresse ab (siehe
`link_by_email` in [`oidc.md`](oidc.md)). Zwei Möglichkeiten:

- In den SCIM-Einstellungen den Anbieter unter **Anmeldung provisionierter Nutzer** auswählen:
  Dann darf genau dieser Anbieter Logins mit *bestätigter* Adresse mit Konten verknüpfen, die
  SCIM angelegt oder übernommen hat – andere Konten bleiben geschützt (empfohlen).
- Oder `link_by_email` am Anbieter selbst einschalten (gilt dann für alle Konten).

## 6. Löschen

`DELETE /Users/{id}` nutzt das Löschkonzept aus [`../PRIVACY.md`](../PRIVACY.md)
(`app.privacy.deletion.delete_user`, #36): Nutzer, eigene Postfächer mit allen Mails, Anhängen,
Suchindex, Todos, Digests, Fragen-Verläufen und Exporten werden gelöscht, Dateien danach.
**Geteilte Postfächer werden nie gelöscht** – nur die Zuweisungen des Nutzers verschwinden.
Gelöschte Gruppen (`DELETE /Groups/{id}`) entfernen nur die Gruppe und die Mitgliedschaften.

Schutz vor Aussperrung: Deaktivieren und Löschen werden mit 409 abgelehnt, wenn danach kein Admin
mehr Zugang hätte (`AdminAccessGuard`) bzw. es der letzte aktive Admin ist.

## 7. Gruppen, Rollen und geteilte Postfächer

Gruppenmitgliedschaften stehen in `scim_group_members`. Die Gruppen eines Nutzers werden
zusätzlich in eine Identität mit Anbieter `scim` gespiegelt (`auth_identities.groups`: Anzeigename
**und** `externalId` jeder Gruppe). Dadurch gilt:

- **Rollen-Mapping (#33):** Regeln mit Anbieter „SCIM“ oder „alle Anbieter“ greifen für
  SCIM-Gruppen. Bei Entra ID kann die Regel die Objekt-ID der Gruppe (`externalId`) oder ihren
  Namen nennen. Ist das Mapping an, wird die Rolle bei jeder Mitgliedschaftsänderung neu bestimmt
  – und auch bei jedem Login zählen die SCIM-Gruppen mit, selbst wenn das Token des Anbieters
  keine Gruppen enthält. Der letzte aktive Admin wird nie herabgestuft.
- **Geteilte Postfächer (#34):** Eine Zuweisung an eine Gruppe (Anbieter „SCIM“ oder alle) gilt
  für alle Mitglieder. Änderungen wirken mit der nächsten Anfrage, nicht erst beim nächsten Login.

## 8. Endpunkte

Basis `/api/scim/v2`, Authentifizierung `Authorization: Bearer <token>`, Inhalte
`application/scim+json` (`application/json` wird ebenfalls angenommen), Fehler im SCIM-Format
(`urn:ietf:params:scim:api:messages:2.0:Error` mit `status`, `scimType`, `detail`).

| Pfad | Methoden |
|---|---|
| `/ServiceProviderConfig`, `/ResourceTypes[/{id}]`, `/Schemas[/{id}]` | GET |
| `/Users` | GET (Filter, Paging), POST |
| `/Users/{id}` | GET, PUT, PATCH, DELETE |
| `/Groups` | GET (Filter, Paging, `excludedAttributes=members`), POST |
| `/Groups/{id}` | GET, PUT, PATCH (Antwort 204), DELETE |

- **Filter:** `eq`, verknüpft mit `and`, auf `userName`, `externalId`, `id`, `emails.value`,
  `displayName`, `active` (Nutzer) bzw. `displayName`, `externalId`, `id`, `members[value eq "…"]`
  (Gruppen). Andere Operatoren (`or`, `co`, `sw`, `pr` …) ergeben 400 `invalidFilter`, statt
  ungenau zu treffen.
- **Paging:** `startIndex` (ab 1) und `count`, höchstens `OLLAMAIL_SCIM_MAX_RESULTS` (200).
- **PATCH:** `add`, `replace`, `remove` in jeder Schreibweise, mit oder ohne `path`, Wertpfade
  wie `emails[type eq "work"].value` und `members[value eq "…"]`.
- Nicht unterstützt: Bulk, Sortierung, ETags, Passwortänderung (siehe `/ServiceProviderConfig`).

## 9. Sicherheit, Rate-Limits, Audit

- Tokens nur als SHA-256 gespeichert, widerrufbar, optional mit Ablauf; `last_used_at` wird
  höchstens minütlich geschrieben.
- Rate-Limits (Fixed Window in Postgres wie beim Login): `OLLAMAIL_SCIM_RATE_LIMIT_PER_MINUTE`
  (600) Anfragen je Token und Minute; `OLLAMAIL_SCIM_FAILED_AUTH_PER_IP` (20) Anfragen ohne
  gültigen Token je IP in 15 Minuten. Darüber 429 mit `Retry-After`.
- Die SCIM-Pfade sind vom CSRF-Schutz ausgenommen (kein Cookie, kein Browser); eine Admin-Session
  ist dort kein gültiger Nachweis.
- **Audit-Log** (nur IDs, Akteur `system`, `via=scim`): `user.created`, `user.updated` (nur die
  Namen der geänderten Felder), `user.deactivated` (mit Anzahl beendeter Sessions),
  `user.reactivated`, `user.deleted`, `user.role_changed`, `group.created`, `group.updated`,
  `group.deleted`, `group.member_added`/`group.member_removed` (Nutzer-ID, Gruppen-ID).
  Einschalten, Linking-Anbieter und Tokens erzeugen/widerrufen als `idp.config_changed`
  (`kind=scim`, Token-ID) mit dem Admin als Akteur.
- Logs enthalten nur IDs und Anzahlen, keine Namen, Adressen oder Gruppennamen.
