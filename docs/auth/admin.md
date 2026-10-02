# Anmeldung und Nutzer verwalten (Admin-Bereich)

Der Admin richtet SSO komplett in der Oberfläche ein: Identity-Provider hinzufügen, Rollen aus
Gruppen ableiten, Nutzer verwalten. Danach melden sich alle Mitarbeitenden mit ihrem bestehenden
Konto an; ein eigenes ollamail-Konto ist nicht nötig.

Code: `backend/app/auth/admin_router.py`, `admin_access.py`, `policy.py`, `invitations.py`,
`backend/app/users/router.py`; Oberfläche: `frontend/src/routes/admin_.sign-in.tsx`,
`admin_.role-mapping.tsx`, `admin_.users.tsx`, `invite.tsx`.

## 1. Admin → Anmeldung

Liste aller Anmeldeverfahren: lokale Konten, OIDC-Provider (Datenbank und
`OLLAMAIL_AUTH_OIDC_PROVIDERS`, letztere nur lesbar) und LDAP-Verzeichnisse.

**Anbieter hinzufügen** (Assistent in drei Schritten):

1. Typ wählen: Microsoft Entra ID, Google Workspace, OpenID Connect (Keycloak, Authentik,
   generisch), LDAP / Active Directory. GitHub wird angezeigt, ist aber erst wählbar, sobald der
   GitHub-Provider vorhanden ist (siehe unten).
2. Daten eintragen. Entra ID: Tenant-ID, Client-ID, Client-Secret (der Issuer wird daraus
   gebildet). Google: Workspace-Domain (`hd`). LDAP: Server, Verschlüsselung, Dienstkonto,
   Suchbasis; die übrigen Felder kommen aus dem Preset des Verzeichnistyps (siehe
   [`ldap.md`](ldap.md)). Der Anbieter wird **deaktiviert** gespeichert.
3. Prüfen und aktivieren. OIDC: Redirect-URI anzeigen und kopieren (beim IdP eintragen), dann
   „Verbindung testen“ (`POST /api/admin/auth/oidc/providers/{name}/test`: Discovery-Dokument
   und Signaturschlüssel werden am Cache vorbei geladen; das Client-Secret prüft erst ein echter
   Login). LDAP: Verbindungstest je Server und optional „Benutzer suchen“. Danach „Jetzt
   aktivieren“ oder später über die Detailansicht.

In der Detailansicht eines Anbieters: Redirect-URI kopieren, Verbindung testen, aktivieren bzw.
deaktivieren, entfernen.

**Lokale Anmeldung abschalten:** `PATCH /api/admin/auth/settings {"local_login_enabled": false}`.
Danach lehnen `POST /api/auth/login`, `POST /api/auth/register` und das Annehmen von Einladungen
mit 403 (`local-login-disabled`) ab; die Login-Seite zeigt nur noch externe Anbieter. Lokale
Passwörter bleiben gespeichert.

**GitHub (#31):** Der Assistent zeigt GitHub an, wählbar wird es, sobald das Backend den Typ in
`GET /api/admin/auth/settings` → `provider_kinds` meldet (`app.state.idp_kinds`, in `main.py`
um `"github"` ergänzen) und der Assistent ein Formular dafür hat. Die Prüfung der Admin-Zugänge
berücksichtigt GitHub automatisch, weil sie alle Provider der `AuthProviderRegistry` einbezieht.

## 2. Schutz vor Selbst-Aussperrung

Serverseitig gilt: **Mindestens ein aktiver Admin muss sich anmelden können.** Ein Admin hat
einen funktionierenden Zugang, wenn sein Konto aktiv ist und er eine Identität bei einem gerade
aktiven Verfahren hat: lokales Passwort bei eingeschalteter lokaler Anmeldung, aktiver
OIDC-/GitHub-Provider (alles in der `AuthProviderRegistry`) oder aktives LDAP-Verzeichnis
(`app/auth/admin_access.py`).

Jede Änderung, die einen Zugang entfernen kann, wird geprüft und mit **409
`urn:ollamail:problem:admin-lockout`** abgelehnt, wenn danach kein Admin mehr Zugang hätte:

| Änderung | Endpunkt |
|---|---|
| Rolle entziehen, Konto deaktivieren (auch das eigene) | `PATCH /api/users/{id}` |
| Lokale Anmeldung abschalten | `PATCH /api/admin/auth/settings` |
| OIDC-Provider deaktivieren/ändern/löschen | `PATCH`/`DELETE /api/admin/auth/oidc/providers/{name}` |
| LDAP-Verzeichnis ändern/löschen | `PUT`/`DELETE /api/auth/ldap/directories/{name}` |

Hatte schon vorher kein Admin Zugang (z. B. nach einer Fehlkonfiguration), werden Änderungen
nicht blockiert, damit sich die Lage schrittweise reparieren lässt.

Bei der Anmeldung über einen externen Anbieter wird der **letzte aktive Admin nie herabgestuft**
(Rollen-Zuordnung oder LDAP-`admin_groups`); der Rollenwechsel wird dann übersprungen und als
`user_role_sync_skipped` geloggt.

Die Oberfläche warnt zusätzlich, bevor sich ein Admin selbst aussperrt: beim Abschalten der
lokalen Anmeldung, wenn er selbst nur lokal angemeldet ist, und bevor er sich selbst die
Admin-Rolle entzieht oder sein Konto deaktiviert. `GET /api/admin/auth/settings` liefert dafür
`admin_access` (Anzahl Admins mit Zugang, eigene Anmeldeverfahren).

## 3. Rollen-Zuordnung (Gruppen → Rollen)

`GET`/`PUT /api/admin/auth/role-mapping`, Oberfläche unter Admin → Rollen-Zuordnung.

- **Aus (Standard):** Rollen werden von Hand in der Nutzerliste vergeben. Ein LDAP-Verzeichnis
  mit `admin_groups` setzt die Rolle seiner Nutzer weiterhin selbst.
- **An:** Bei **jeder** Anmeldung über einen externen Anbieter (OIDC, LDAP, GitHub) wird die Rolle
  zentral in `app/auth/provisioning.py` → `app.auth.policy.resolve_role` bestimmt: die höchste
  Rolle aller Regeln, deren Gruppe der Nutzer hat; ohne Treffer die **Standardrolle**. Eine
  Rolle, die der Anbieter selbst ableitet (LDAP-`admin_groups`), zählt wie eine passende Regel.
  Von Hand vergebene Rollen werden dabei für diese Nutzer überschrieben; lokale Konten sind
  nicht betroffen.
- **Regeln:** Gruppe, optional Anbieter (`oidc:entra`, `ldap:ad`; leer = alle), Rolle. Gruppen
  werden ohne Beachtung der Groß-/Kleinschreibung verglichen und so eingetragen, wie der
  Anbieter sie meldet: Entra ID die Objekt-ID der Gruppe (Claim `groups`), LDAP der Gruppen-DN,
  Keycloak/Authentik der Gruppenname bzw. -pfad. Je Anbieter darf eine Gruppe nur eine Regel
  haben (sonst 422).
- **Ausprobieren:** `POST /api/admin/auth/role-mapping/test {"provider", "groups"}` zeigt, welche
  Rolle eine Anmeldung mit diesen Gruppen bekäme.
- Ein Gruppenwechsel im IdP wirkt bei der **nächsten Anmeldung**; laufende Sitzungen behalten
  ihre Rolle nicht: jede Anfrage prüft die aktuelle Rolle aus der Datenbank.

Speichern wird protokolliert (`idp.config_changed`, `details.kind = role_mapping`, Anzahl Regeln
und Admin-Regeln, keine Gruppennamen); jeder Rollenwechsel beim Login als `user.role_changed`
(Akteur `system`).

## 4. Nutzerverwaltung

Admin → Nutzer (`/api/users`): Liste mit Rolle, Anmeldeverfahren, Status (aktiv, eingeladen,
deaktiviert), letzter Anmeldung und Zahl der aktiven Sitzungen. **Keine Einsicht in Mails**:
die API liefert nur Kontodaten (docs/PRIVACY.md, „Admin ≠ Leser“).

| Aktion | Endpunkt | Audit |
|---|---|---|
| Rolle ändern | `PATCH /api/users/{id} {"role"}` | `user.role_changed` (`via: admin`) |
| Deaktivieren / reaktivieren | `PATCH /api/users/{id} {"is_active"}` | `user.deactivated` (beendet alle Sitzungen) / `user.reactivated` |
| Überall abmelden | `DELETE /api/users/{id}/sessions` | `auth.session_revoked` (`via: admin`) |
| Lokalen Nutzer einladen | `POST /api/users/invitations` | `user.created` (`via: invitation`), `user.invited` |
| Neuer Einladungslink | `POST /api/users/{id}/invitation` | `user.invited` (`renewed`) |

**Einladungen:** Das Konto wird mit lokaler Identität ohne Passwort angelegt. Der Admin erhält
einmalig einen Link `https://<host>/invite#<token>` und gibt ihn weiter (ollamail verschickt
selbst keine Mails). Der Token (256 Bit) steht im URL-Fragment und erreicht so weder Server-Logs
noch `Referer`; gespeichert wird nur sein SHA-256. Gültigkeit:
`OLLAMAIL_AUTH_INVITATION_LIFETIME_HOURS` (Standard 168). Auf der Seite setzt die Person ihr
Passwort (`POST /api/auth/invitations/accept`) und ist danach angemeldet (`user.password_set`,
`via: invitation`). Ein neuer Link ersetzt den alten. Bei abgeschalteter lokaler Anmeldung sind
Einladungen nicht möglich.

## 5. Notfallzugang

Wenn keine Anmeldung mehr funktioniert (IdP ausgefallen, Fehlkonfiguration):

```sh
# Neues lokales Passwort für ein vorhandenes Konto (auch SSO-Nutzer):
docker compose -f deploy/compose.yaml run --rm api python -m app.cli reset-password
# Neues lokales Admin-Konto:
docker compose -f deploy/compose.yaml run --rm api python -m app.cli create-admin
```

Beide fragen das Passwort verdeckt ab und **schalten die lokale Anmeldung wieder ein**, falls sie
abgeschaltet war (`idp.config_changed`, `via: cli`). `reset-password` beendet außerdem alle
Sitzungen des Nutzers, löscht eine offene Einladung und die Login-Sperre; mit `--activate`
reaktiviert es ein deaktiviertes Konto. Die Rolle ändert es nicht.
