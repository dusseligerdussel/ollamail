# Roadmap

Die Arbeit ist in GitHub-Issues geschnitten, damit mehrere Agenten parallel arbeiten können.
Jedes Issue nennt seine Abhängigkeiten. Phasen werden über Labels (`phase:0-foundation`, …) abgebildet.

## Phase 0 – Fundament
Monorepo-Gerüst, CI, Docker Compose, Datenbank, Konfiguration, Krypto, Logging, Job-Queue, Frontend-Shell, Design-System.

## Phase 1 – MVP
- Auth: Erst-Admin-Bootstrap, lokale Anmeldung, Sessions, Rollen
- Mail: IMAP-Postfächer, Initialimport, IDLE-Sync, Mail-Ansicht
- KI: LLM-Provider-Abstraktion (Ollama + OpenAI-kompatibel), Modellprofile
- Triage, Todos, Embeddings + Hybrid-Suche, RAG-Chat mit Zitaten
- Daily Digest mit Piper-TTS, Web-Player und Podcast-Feed

## Phase 2 – Enterprise (v1)
- OIDC (Entra ID, Google, generisch), GitHub-OAuth, LDAP/AD, Gruppen-Rollen-Mapping, JIT-Provisioning
- Shared Mailboxes, Audit-Log, Aufbewahrungsfristen, Datenexport/Löschung
- Microsoft 365 (Graph) und Gmail/Google Workspace als Mail-Provider
- Multi-Arch-Images, Betriebsdoku, Backups

## Phase 3 – Später
- Todo-Export (CalDAV, Microsoft To Do, Google Tasks)
- Antwortentwürfe
- SAML, SCIM, WebAuthn
- Helm-Chart
- Paperless-ngx-Anbindung (Anhänge automatisch ablegen) – bewusst zurückgestellt
- OCR für gescannte Anhänge
