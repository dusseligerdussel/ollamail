# LDAP / Active Directory

Unternehmen mit einem lokalen Active Directory oder einem anderen LDAP-Verzeichnis (OpenLDAP,
389 DS, FreeIPA, Samba AD) melden ihre Nutzer direkt mit dem Verzeichnis-Passwort an. Beim
ersten Login wird der Nutzer in ollamail angelegt (Just-in-Time-Provisioning); die Rolle kann
über Gruppen gesteuert werden.

Code: `backend/app/auth/providers/ldap/`, Provisioning: `backend/app/auth/provisioning.py`.
Die Admin-Oberfläche folgt mit #33; bis dahin wird über die API konfiguriert.

## Ablauf einer Anmeldung

1. `POST /api/auth/login/ldap/{name}` mit `{"username": "...", "password": "..."}`.
2. Rate-Limit wie beim lokalen Login: Zähler pro Client-IP und pro Konto
   (`ldap:<name>:<username>`), sonst `429` mit `retry_after`.
3. Verbindung zum ersten erreichbaren Server der Liste (Failover), TLS-Handshake mit Prüfung von
   Zertifikatskette und Hostname, Bind als Service-Account.
4. Suche nach dem Nutzer mit `user_filter`. Genau **ein** Treffer ist nötig; bei mehreren
   Treffern wird die Anmeldung abgelehnt.
5. Konto deaktiviert (`userAccountControl` mit Bit `ACCOUNTDISABLE`)? → Ablehnung.
6. Bind als Nutzer mit dem gefundenen DN und dem eingegebenen Passwort (neue Verbindung).
7. Gruppen auflösen (siehe unten), `allowed_groups` und `admin_groups` anwenden.
8. Nutzer über `auth_identities` (`provider = ldap:<name>`, `subject` = `objectGUID` bzw.
   `entryUUID`) finden oder anlegen, Session starten.

Falsches Passwort, unbekanntes, deaktiviertes oder nicht berechtigtes Konto führen alle zu
derselben Antwort (`401`). Unbekannte und deaktivierte Konten kosten trotzdem einen Bind (mit
einem nicht existierenden DN), damit die Antwortzeit nichts verrät. Ist kein Server erreichbar,
antwortet die API mit `503` statt `401`.

| Status | Bedeutung |
|---|---|
| `200` | Angemeldet, Session-Cookie gesetzt |
| `401` | Zugangsdaten falsch, Konto deaktiviert/nicht berechtigt oder Nutzer in ollamail deaktiviert |
| `403` | Das Verzeichnis liefert keine E-Mail-Adresse für den Nutzer |
| `404` | Verzeichnis unbekannt oder deaktiviert |
| `409` | Die E-Mail-Adresse gehört bereits einem anderen ollamail-Konto (siehe unten) |
| `429` | Zu viele Versuche |
| `503` | Verzeichnis nicht erreichbar oder Konfiguration defekt |

## Sicherheit

- **TLS ist Pflicht.** `tls_mode` ist `ldaps` (Standard, `ldaps://…:636`) oder `starttls`
  (`ldap://…:389`, StartTLS vor jedem Bind). Zertifikat und Hostname werden immer geprüft,
  mindestens TLS 1.2. Für eine interne CA wird ihr Zertifikat (PEM) in `ca_certificate`
  hinterlegt, sonst gelten die System-CAs. `tls_mode: "none"` wird nur akzeptiert, wenn
  `OLLAMAIL_AUTH_LDAP_ALLOW_PLAINTEXT=true` gesetzt ist; wird die Variable später entfernt,
  schlagen Logins über solche Verzeichnisse mit `503` fehl.
- **Kein Unauthenticated Bind.** Ein leeres Passwort würde bei vielen Servern als erfolgreicher
  anonymer Bind gelten (RFC 4513, 5.1.2). Leere Passwörter und Passwörter mit NUL-Byte erreichen
  das Verzeichnis nie.
- **LDAP-Injection.** Der Login-Name wird nur als Wert in den Filter eingesetzt: Jedes Zeichen
  außer `A–Z a–z 0–9 . _ - @` und Leerzeichen wird hex-escaped (RFC 4515), auch `*`, `(`, `)`,
  `\`, NUL, `=`, `~` und Umlaute. Das gilt ebenso für DNs in Gruppenfiltern. Der Platzhalter
  `{login}` wird per String-Ersetzung gefüllt, nicht mit `str.format`. Tests:
  `tests/auth/ldap/test_filters.py` und gegen einen echten Server `tests/auth/ldap/test_client.py`.
- **Keine Referrals.** Verweise auf andere Server werden nicht verfolgt; Passwörter gehen nur an
  die konfigurierten Server.
- **Bind-Passwort** des Service-Accounts verschlüsselt (`EncryptedStr`, AES-256-GCM, siehe
  `PRIVACY.md`), die API gibt es nie zurück (`bind_password_set: true`).
- **Keine Kontoübernahme per E-Mail-Adresse.** Existiert schon ein Konto mit der Adresse aus dem
  Verzeichnis (z. B. ein lokales Admin-Konto), wird es nicht automatisch verknüpft, sondern der
  Login endet mit `409`. Sonst könnte jeder, der im Verzeichnis ein `mail`-Attribut setzen darf,
  fremde Konten übernehmen.
- **Logs** enthalten weder Login-Namen noch DNs oder Gruppen, nur Provider, Nutzer-ID und
  Fehlercodes.
- Wird ein Konto im AD deaktiviert, bleiben bestehende ollamail-Sessions bis zum Idle-Timeout
  bzw. Ablauf gültig. Sofort sperren: Nutzer in ollamail deaktivieren oder seine Sessions
  widerrufen.

## Konfiguration (Admin-API)

Alle Endpunkte erfordern die Rolle `admin`.

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/api/auth/ldap/directories` | Alle Verzeichnisse |
| `POST` | `/api/auth/ldap/directories` | Verzeichnis anlegen |
| `GET` | `/api/auth/ldap/directories/{name}` | Ein Verzeichnis |
| `PUT` | `/api/auth/ldap/directories/{name}` | Konfiguration ersetzen; ohne `bind_password` bleibt das gespeicherte |
| `DELETE` | `/api/auth/ldap/directories/{name}` | Verzeichnis löschen; die Verknüpfungen (`auth_identities`) der Nutzer werden entfernt, die Nutzer bleiben |
| `POST` | `/api/auth/ldap/directories/{name}/test` | **Verbindung testen:** jeder Server wird verbunden, TLS ausgehandelt und der Service-Account gebunden |
| `POST` | `/api/auth/ldap/directories/{name}/test-user` | **Testnutzer suchen:** `{"login": "..."}` → DN, ID, E-Mail, Anzeigename, Gruppen, deaktiviert, berechtigt, Rolle (ohne Passwortprüfung) |

Fehlercodes im Verbindungstest: `unreachable`, `tls_failed`, `starttls_failed`,
`connection_lost`, `service_bind_failed`, `bind_failed`; bei der Nutzersuche zusätzlich
`search_failed` und `subject_attribute_missing`.

Beispiel Active Directory:

```json
{
  "name": "corp",
  "display_name": "Firmenkonto",
  "enabled": true,
  "bind_password": "<Passwort des Service-Accounts>",
  "settings": {
    "directory_type": "active_directory",
    "server_urls": ["ldaps://dc1.corp.example:636", "ldaps://dc2.corp.example:636"],
    "tls_mode": "ldaps",
    "ca_certificate": "-----BEGIN CERTIFICATE-----\n…\n-----END CERTIFICATE-----\n",
    "bind_dn": "CN=svc-ollamail,OU=Service Accounts,DC=corp,DC=example",
    "user_base_dn": "OU=Staff,DC=corp,DC=example",
    "group_base_dn": "OU=Groups,DC=corp,DC=example",
    "allowed_groups": ["CN=ollamail-users,OU=Groups,DC=corp,DC=example"],
    "admin_groups": ["CN=ollamail-admins,OU=Groups,DC=corp,DC=example"]
  }
}
```

Der Name (`[a-z0-9-]`, höchstens 32 Zeichen) ist Teil des Providers `ldap:<name>` und der
Login-URL und kann nicht geändert werden.

### Einstellungen

Felder ohne Angabe übernehmen den Wert des Presets von `directory_type`.

| Feld | `active_directory` | `openldap` | Bedeutung |
|---|---|---|---|
| `server_urls` | – | – | 1–10 URLs, in dieser Reihenfolge probiert (Failover) |
| `tls_mode` | `ldaps` | `ldaps` | `ldaps`, `starttls` oder `none` (siehe oben); muss zum URL-Schema passen |
| `ca_certificate` | – | – | PEM; leer = System-CAs |
| `bind_dn` | – | – | DN des Service-Accounts (nur Lesezugriff nötig) |
| `user_base_dn` | – | – | Basis der Nutzersuche |
| `user_filter` | `(&(objectCategory=person)(objectClass=user)(\|(sAMAccountName={login})(userPrincipalName={login})))` | `(&(objectClass=inetOrgPerson)(uid={login}))` | Muss `{login}` enthalten |
| `subject_attribute` | `objectGUID` | `entryUUID` | Stabile ID; ändert sich nicht beim Umbenennen oder Verschieben |
| `email_attribute` | `mail` | `mail` | Pflicht für das Anlegen des Nutzers |
| `display_name_attribute` | `displayName` | `cn` | Anzeigename beim Anlegen |
| `group_base_dn` | = `user_base_dn` | = `user_base_dn` | Basis der Gruppensuche |
| `group_filter` | `(objectClass=group)` | `(\|(objectClass=groupOfNames)(objectClass=groupOfUniqueNames))` | Welche Objekte Gruppen sind |
| `group_member_attribute` | `member` | `member` | Attribut mit den Mitglieds-DNs |
| `nested_groups` | `true` | `true` | Verschachtelte Gruppen einbeziehen |
| `allowed_groups` | `[]` | `[]` | Nur Mitglieder dieser Gruppen dürfen sich anmelden (leer = alle) |
| `admin_groups` | `[]` | `[]` | Mitglieder werden `admin`, alle anderen `user` |
| `connect_timeout` | `5` | `5` | Sekunden pro Server für den Verbindungsaufbau |
| `operation_timeout` | `10` | `10` | Sekunden pro LDAP-Operation |

### Gruppen und Rollen

- **Active Directory:** Mit `nested_groups` sucht ollamail alle Gruppen mit
  `(member:1.2.840.113556.1.4.1941:=<Nutzer-DN>)` (`LDAP_MATCHING_RULE_IN_CHAIN`); der
  Domain-Controller löst die Verschachtelung selbst auf.
- **Andere Verzeichnisse:** Gruppen werden Ebene für Ebene gesucht (`member=<DN>`), höchstens
  10 Ebenen und 1000 Gruppen; Zyklen sind unschädlich.
- Gruppen-DNs werden normalisiert verglichen (Groß-/Kleinschreibung, Leerzeichen um `,`/`=`).
  Am einfachsten übernimmt man sie aus dem Ergebnis von „Testnutzer suchen“.
- Ist `admin_groups` gesetzt, wird die Rolle **bei jedem Login** aus den Gruppen abgeleitet (auch
  zurückgestuft). Ist es leer, bekommen neue Nutzer die Rolle `user`, und Rollenänderungen in
  ollamail bleiben erhalten.
- Gruppen werden nicht gespeichert, nur bei der Anmeldung ausgewertet.
- Die primäre AD-Gruppe (`primaryGroupID`, meist „Domain Users“) taucht in `member` nicht auf
  und kann deshalb nicht in `allowed_groups`/`admin_groups` verwendet werden.

## Tests

`tests/auth/ldap/` enthält Unit-Tests (Escaping, Validierung) und Integrationstests gegen einen
echten OpenLDAP-Server: `tests/auth/ldap/slapd.py` startet `slapd` mit eigener CA auf freien
Ports (LDAPS und StartTLS) und lädt fiktive Testdaten. Lokal: `apt-get install slapd`; ohne
`slapd` werden diese Tests übersprungen. Die CI installiert `slapd` und setzt
`OLLAMAIL_TEST_REQUIRE_LDAP=1`. Die AD-Spezifika (`objectGUID`, `userAccountControl`,
`LDAP_MATCHING_RULE_IN_CHAIN`) sind per Unit-Test bzw. über das `msuser`-Schema von OpenLDAP
abgedeckt; ein Samba-AD-Test ist nicht enthalten.
