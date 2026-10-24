# Proxmox Dashboard

Selbst gehostetes Dashboard für Proxmox-VE-Cluster und Standalone-Hosts. Es zeigt die Auslastung von Nodes und Gästen, beantwortet die Frage „Woher kommt die Last?“, inventarisiert die Hardware, überwacht Backups und Storage, alarmiert bei Problemen und erlaubt eine abgesicherte Steuerung. Dazu kommen eine dokumentierte REST-API, ein Prometheus-Endpunkt und ein MCP-Server für KI-Assistenten.

**Version 1.0.0** · Backend Python 3.13 / FastAPI · Frontend SvelteKit 3 / Svelte 5 / Tailwind 4 / ECharts · VictoriaMetrics für die Historie · SQLite für Benutzer, Audit-Log und Regeln

[![CI](https://github.com/phillipunzen/Proxmox-Dashboard/actions/workflows/ci.yml/badge.svg)](https://github.com/phillipunzen/Proxmox-Dashboard/actions/workflows/ci.yml)

**Images (amd64 + arm64):**
- `ghcr.io/phillipunzen/proxmox-dashboard`
- `ghcr.io/phillipunzen/proxmox-dashboard-mcp`

Tags: `latest` (main), `1.0.0` / `1.0` / `1` (Releases), `sha-…`.

---

## Inhalt

1. [Funktionen](#funktionen)
2. [Architektur](#architektur)
3. [Schnellstart mit Beispieldaten](#schnellstart-mit-beispieldaten)
4. [Installation mit echtem Cluster](#installation-mit-echtem-cluster)
5. [Proxmox-Rollen und Tokens](#proxmox-rollen-und-tokens)
6. [SSH-Zugang zu den Nodes](#ssh-zugang-zu-den-nodes-optional)
7. [Anmeldung, Rollen, Sicherheit](#anmeldung-rollen-sicherheit)
8. [Steuerung](#steuerung)
9. [Node-Verwaltung](#node-verwaltung)
10. [Backups, Snapshots, Storage-Prognose](#backups-snapshots-storage-prognose)
11. [Alerting](#alerting)
12. [API, API-Keys, Prometheus](#api-api-keys-prometheus)
13. [MCP-Server für KI-Assistenten](#mcp-server-für-ki-assistenten)
14. [Backup und Update des Dashboards](#backup-und-update-des-dashboards)
15. [Konfiguration (Referenz)](#konfiguration-referenz)
16. [Entwicklung](#entwicklung)
17. [Fehlersuche](#fehlersuche)

---

## Funktionen

| Bereich | Was |
|---|---|
| **Übersicht** | Nodes (online, Quorum, Uptime, Version, Kernel), Summen für CPU, RAM, Swap, Storage, Netz, Disk-IO, IO-Delay und Load, Gästezahlen, Warnungen. Alles live per Server-Sent Events. |
| **Lastanalyse** | Gestapelter Anteil der Top-Gäste an der Host-Last. Ein Klick auf eine Lastspitze zeigt, wer zu diesem Zeitpunkt verantwortlich war. Top-Liste mit Ø, p95 und Max, Treemap, Overcommitment (vCPU und RAM: zugewiesen vs. physisch). Filter nach Node, Typ, Tag, Pool, Zeitraum (1h bis 30d). |
| **Historie** | VictoriaMetrics, 400 Tage, Werte alle 10 s. Verlaufs-Charts für Cluster, Node und Gast. |
| **Inventar** | CPU-Topologie, RAM, Mainboard (per SSH), Datenträger mit SMART und Verschleiß, NICs, Bonds, Bridges, VLANs, PCI-Geräte mit Passthrough-Zuordnung (auch über PCI-Mappings), Gast-Konfigurationen und Guest-Agent-Daten. Export als CSV oder JSON. |
| **Backups** | Letztes Backup je Gast, Alter, Verify-Status, welche Backup-Jobs einen Gast erfassen, Gäste ohne Job, Snapshots samt Alter. |
| **Storage** | Belegung, Trend und Prognose „voll in ca. N Tagen“. |
| **Steuerung** | Start, Herunterfahren, Neustart, Pause, hart Stoppen. Snapshot erstellen, zurücksetzen, löschen. Backup sofort, Migration mit Vorab-Prüfung, Bulk-Aktionen nach Auswahl oder Tag, Konsolen-Link. Proxmox-Aufgaben mit Live-Log. |
| **Node-Verwaltung** | Update-Liste mit Changelog, `dist-upgrade` per SSH mit Live-Ausgabe, Wartungsmodus mit Evakuierung, Neustart mit Quorum-Prüfung. Optional Paket-Updates in Gästen. |
| **Alerting** | Regeln mit Schwellwert, Mindestdauer und Wiederholung. Kanäle: ntfy, Gotify, E-Mail, Webhook, Home Assistant. Historie und Bestätigen. |
| **Sicherheit** | Lokale Benutzer mit Argon2id und TOTP, optional OIDC. Rollen Viewer, Operator, Admin. Audit-Log, CSRF-Schutz, Rate-Limiting, CSP, Read-only- und Dry-Run-Modus. |
| **Schnittstellen** | REST-API `/api/v1` mit OpenAPI-Doku, API-Keys mit Scopes, Prometheus `/metrics`, MCP-Server (stdio und Streamable HTTP). |
| **Entwicklung** | Mock-Modus mit realistischem Cluster, 30 Tagen Historie, simulierten Tasks und SSH. |

## Architektur

```
                 ┌──────────────────────── docker compose ─────────────────────────────┐
 Browser ──────► │  dashboard (FastAPI, 1 Prozess)                                     │
 Home Assistant ►│   ├─ /api/v1/*  REST + OpenAPI (/api/docs), SSE-Live-Stream         │
 Prometheus ───► │   ├─ /metrics                                                       │
                 │   ├─ SPA (SvelteKit, statisch)                                      │
 KI-Assistent ─► │   ├─ Collector ──► Live-Snapshot ──► VictoriaMetrics (Historie)     │
   (MCP) ──┐     │   ├─ Inventar · Backups · Alert-Engine · Task-Watcher · Jobs        │
           │     │   └─ SQLite (/data): Benutzer, Sessions, Audit, Regeln, API-Keys    │
           └───► │  mcp (optional) ── nutzt die REST-API mit einem API-Key             │
                 └────────┬──────────────────────────────────────┬──────────────────────┘
                          │ HTTPS + API-Tokens                   │ SSH (optional, eingeschränkt)
                          ▼                                      ▼
                   Proxmox VE API (Failover über alle Nodes)   Nodes: Upgrade, DMI, Sensoren
```

- **Ein Collector für alle Clients.** `/cluster/resources` liefert alle 10 s Nodes, Gäste und Storage in einem Aufruf. Die Last auf Proxmox ist unabhängig davon, wie viele Dashboards oder Tools gerade offen sind.
- **Drei getrennte Proxmox-Tokens:**
  - ReadOnly: alles Lesende
  - Operator: Gast-Aktionen
  - NodeAdmin: Updates und Node-Neustart

  Fehlt ein Token, bleibt der jeweilige Bereich gesperrt.
- **Host-Anteil der Gäste:** Proxmox meldet die CPU eines Gasts relativ zu seinen vCPUs. Für die Lastanalyse rechnet das Dashboard in belegte Host-Threads um (`cpu × vCPUs`). Ein Gast mit 50 % auf 4 vCPUs belegt also 2 Threads. Die Lücke zwischen der Summe der Gäste und dem Host-Wert ist Last des Hosts selbst (ZFS, Backups, Kernel).

## Schnellstart mit Beispieldaten

```bash
git clone https://github.com/phillipunzen/Proxmox-Dashboard.git && cd Proxmox-Dashboard
cp .env.example .env
sed -i 's/^PVE_MOCK=false/PVE_MOCK=true/; s/^READ_ONLY=true/READ_ONLY=false/' .env
docker compose up -d                                    # lädt die fertigen Images von ghcr.io
docker compose logs dashboard | grep "Initial admin"   # Startpasswort
```

Dann `http://<host>:8080` öffnen und als `admin` mit dem Startpasswort anmelden. Das Dashboard führt durch TOTP-Einrichtung und Passwortwechsel.

**Was der Mock simuliert:**
- **Cluster:** `homelab` mit 3 Nodes, 13 VMs und 20 Containern sowie den Standalone-Host `edge`.
- **Last und Historie:** Lastkurven mit Tagesverlauf und Spitzen, 30 Tage Historie.
- **Hardware:** Eine defekte SSD, GPU- und HBA-Passthrough.
- **Backups:** Backup-Jobs, ein fehlgeschlagenes Backup, alte Snapshots.
- **Updates:** Verfügbare Updates mit Kernel-Update.
- **Simulierte Aktionen:** Tasks, Migrationen und Reboots laufen realistisch ab, inklusive Fehlern.

## Installation mit echtem Cluster

**Voraussetzungen:**
- Docker mit Compose v2, z. B. in einem LXC mit `nesting=1` oder in einer VM.
- Netzwerkzugriff auf Port 8006 der Nodes, für SSH-Funktionen zusätzlich auf Port 22.

```bash
git clone https://github.com/phillipunzen/Proxmox-Dashboard.git /opt/proxmox-dashboard
cd /opt/proxmox-dashboard
cp .env.example .env && chmod 600 .env
```

Das Repository brauchst du für `docker-compose.yml` und die Skripte in `deploy/`. Die Images selbst kommen fertig gebaut von ghcr.io. Für vorhersehbare Updates pinnst du die Version in `.env`: `DASHBOARD_VERSION=1.0.0`. Der Standard `latest` folgt dem main-Branch.

1. **Rollen und Tokens anlegen**, siehe [nächster Abschnitt](#proxmox-rollen-und-tokens).
2. **`.env` ausfüllen:**
   - Mindestens `PVE_1_NAME`, `PVE_1_URLS` (alle Nodes, kommagetrennt, für Failover), `PVE_1_TOKEN_ID` und `PVE_1_TOKEN_SECRET`.
   - Für Steuerung und Node-Verwaltung zusätzlich `PVE_1_OPERATOR_TOKEN_*`, `PVE_1_NODEADMIN_TOKEN_*` und `READ_ONLY=false`.
   - Weitere Cluster oder Standalone-Hosts kommen als `PVE_2_*`, `PVE_3_*` usw. dazu.
3. **TLS:** Lass die Zertifikatsprüfung an. Bei selbstsignierten Zertifikaten bindest du die Cluster-CA ein:
   ```bash
   scp root@pve1:/etc/pve/pve-root-ca.pem certs/
   ```
   In `.env` trägst du dann `PVE_1_CA_FILE=/certs/pve-root-ca.pem` ein. `PVE_1_VERIFY_TLS=false` ist nur eine Notlösung.
4. **Starten:**
   ```bash
   docker compose up -d
   docker compose logs dashboard | grep "Initial admin"
   ```
5. **Hinter einem Reverse-Proxy** (Traefik, Caddy, NPM):
   - Proxy-IP in `FORWARDED_ALLOW_IPS` eintragen.
   - Öffentliche URL in `DASHBOARD_URL` eintragen.
   - Buffering für SSE abschalten. Nginx: `proxy_buffering off;`. Das Dashboard sendet bereits `X-Accel-Buffering: no`.

> Wechsel vom Mock zum echten Cluster: Lösche die Mock-Historie mit `docker compose down && docker volume rm proxmox-dashboard_vm-data`. Das Volume `app-data` mit Benutzern und Audit-Log bleibt dabei erhalten.

## Proxmox-Rollen und Tokens

Das Skript auf **einem** Node als root ausführen. Rollen und ACLs gelten clusterweit:

```bash
bash deploy/pve-setup-roles.sh                       # nur Monitoring
bash deploy/pve-setup-roles.sh --operator            # + Gast-Steuerung
bash deploy/pve-setup-roles.sh --node-admin          # + Updates, Node-Neustart (impliziert --operator)
bash deploy/pve-setup-roles.sh --node-admin --guest-updates   # + apt in VMs über den Guest Agent
```

| Rolle / Token | Privilegien | Wofür |
|---|---|---|
| `Dashboard-ReadOnly` → `dashboard@pve!readonly` | `Sys.Audit`, `VM.Audit`, `Datastore.Audit`, `Pool.Audit`, `SDN.Audit`, `Mapping.Audit`, `VM.GuestAgent.Audit` | Status, Inventar, SMART, Tasks, Backups. Guest-Agent-Infos (`VM.Monitor` gibt es seit PVE 9 nicht mehr). |
| `Dashboard-Operator` → `…!operator` | + `VM.PowerMgmt`, `VM.Snapshot`, `VM.Snapshot.Rollback`, `VM.Backup`, `VM.Migrate`, `Datastore.AllocateSpace` | Gast-Aktionen, Migration im Wartungsmodus |
| `Dashboard-NodeAdmin` → `…!nodeadmin` | + `Sys.Modify`, `Sys.PowerMgmt` | Update-Liste (Proxmox verlangt `Sys.Modify` schon zum **Auflisten**), `apt-get update`, Changelog, Node-Neustart |
| mit `--guest-updates` | + `VM.GuestAgent.Unrestricted` | apt in VMs. Achtung: Das entspricht root im Gast. |

Alle Tokens werden mit `privsep=1` angelegt, haben also nur die Rechte ihrer eigenen ACL. Das Skript ist idempotent. Ein erneuter Lauf aktualisiert die Rollen, die Secrets bleiben unverändert.

Rechte prüfen:

```bash
pveum user permissions 'dashboard@pve!readonly' --path /
```

> **Hinweis zu Backups:** Je nach PVE-Version filtert Proxmox die Backup-Liste eines Storages nach Rechten. Bleibt die Backup-Übersicht leer, gib dem ReadOnly-Token auf dem Backup-Storage zusätzlich `Datastore.AllocateSpace`, z. B. `pveum acl modify /storage/pbs --tokens 'dashboard@pve!readonly' --roles PVEDatastoreUser`.

## SSH-Zugang zu den Nodes (optional)

SSH wird nur für das gebraucht, was die API nicht kann:
- Paket-Upgrade mit Live-Ausgabe
- Mainboard-Daten
- Temperaturen
- Prüfung „Neustart erforderlich“
- optional Updates in Containern

Einrichtung:

1. Den öffentlichen Schlüssel kopierst du unter **Admin → SSH**. Das Dashboard erzeugt das Schlüsselpaar beim ersten Start in `/data/ssh/`.
2. Auf **jedem Node** als root:
   ```bash
   bash deploy/node-setup.sh "ssh-ed25519 AAAA… pve-dashboard"
   ```
3. In **Admin → SSH** den Host-Key abrufen, mit dem ausgegebenen Fingerprint vergleichen, vertrauen und anschließend „Testen“.

**Was das Skript einrichtet:**
- **Benutzer:** `pve-dashboard` ohne Passwort, Anmeldung nur per Key.
- **Erzwungener Befehl:** `authorized_keys` mit `restrict,command="/usr/local/sbin/pve-dashboard-ssh"`. Keine Shell, kein Forwarding.
- **Feste Befehlsliste:** Der Dispatcher akzeptiert nur `upgrade`, `update`, `dmi`, `sensors`, `reboot-required` und `version`. Für Gast-Updates kommen `lxc-check <vmid>` und `lxc-upgrade <vmid>` dazu, wobei die VMID strikt numerisch geprüft wird.
- **Eine sudo-Regel** für ein root-eigenes Hilfsskript.
- **Host-Keys:** Das Dashboard verbindet sich nur zu Host-Keys, die du bestätigt hast. Ändert sich ein Key, bricht es die Verbindung ab.

> Ehrlich: `upgrade` installiert Pakete als root, und Maintainer-Skripte laufen als root. Wer den Dashboard-Schlüssel besitzt, kann damit Updates aus deinen Repositories einspielen, aber keine beliebigen Befehle ausführen. Schütze das Volume `app-data` und die Backups entsprechend.

## Anmeldung, Rollen, Sicherheit

| Rolle | Darf |
|---|---|
| **Viewer** | alles sehen, eigene Audit-Einträge |
| **Operator** | zusätzlich Gäste steuern, Snapshots, Backups, Migration, Bulk, Alarme bestätigen, Gast-Updates |
| **Admin** | zusätzlich Node-Verwaltung, Alarm-Regeln und -Kanäle, SSH, Benutzer, API-Keys, komplettes Audit-Log |

**Lokale Benutzer:**
- **Passwörter:** Argon2id-Hash, mindestens 12 Zeichen. Neue oder zurückgesetzte Benutzer müssen ihr vorläufiges Passwort beim ersten Login ändern.
- **TOTP (RFC 6238):** Ein Code kann nicht zweimal verwendet werden, das Geheimnis wird verschlüsselt gespeichert. Pflicht für die Rollen in `TOTP_REQUIRED_ROLES` (Standard: `admin,operator`). Bei Geräteverlust setzt ein Admin TOTP zurück.
- **Sperre:** Nach 5 Fehlversuchen ist das Konto 15 Minuten gesperrt. Zusätzlich gilt ein Rate-Limit pro IP (20 in 5 Minuten) und pro Benutzer (10 in 5 Minuten). Die Fehlermeldung verrät nicht, ob ein Benutzer existiert.

**OIDC / Single Sign-on (optional),** z. B. Authentik, Keycloak oder Cloudflare Access als OIDC-Provider:

1. Beim IdP eine Anwendung anlegen: confidential client, Redirect-URI `<DASHBOARD_URL>/api/v1/auth/oidc/callback`, Scopes `openid profile email groups`.
2. In `.env` eintragen: `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, dazu `OIDC_ADMIN_GROUPS`, `OIDC_OPERATOR_GROUPS` und `OIDC_VIEWER_GROUPS`. Eine leere Viewer-Gruppe bedeutet: Jeder IdP-Benutzer darf lesen.
3. Auf der Login-Seite erscheint dann „Mit SSO anmelden“.

So verhält sich der SSO-Login:
- **Ablauf:** Authorization Code mit PKCE, State und Nonce. Das ID-Token wird über JWKS geprüft (Signatur, Issuer, Audience, Ablauf, Nonce).
- **Rollen:** Sie werden bei jedem Login aus den IdP-Gruppen übernommen.
- **MFA** übernimmt der IdP, das Dashboard fragt kein eigenes TOTP ab.
- **Lokaler Login:** Für SSO-Benutzer ist er gesperrt.
- **Namenskollision:** Ein IdP-Benutzer mit dem Namen eines lokalen Kontos wird nur mit `OIDC_LINK_EXISTING=true` verknüpft.

**Weitere Schutzmaßnahmen:**
- **Sitzungen:** Serverseitig gespeichert, im Cookie steht nur ein Zufallstoken. Das Cookie ist `HttpOnly` und `SameSite=Strict`, über HTTPS zusätzlich `Secure`. Eine Sitzung gilt maximal 12 h und endet nach 2 h Inaktivität. Ein Passwortwechsel beendet die anderen Sitzungen.
- **Schreibende Anfragen:** brauchen das `X-CSRF-Token`, zusätzlich wird der Origin geprüft.
- **Header:** CSP (Hash für das einzige Inline-Script), `X-Frame-Options: DENY`, `nosniff`.
- **Secrets:** Kanal-Zugangsdaten, TOTP und die PVE-Tokens in `.env` stehen nie im Code. Gespeicherte Geheimnisse sind verschlüsselt (Schlüssel `APP_SECRET_KEY` bzw. `/data/secret.key`).
- **Audit-Log:** Erfasst Logins, alle Aktionen (wer, was, wann, IP, Parameter, Proxmox-Ergebnis), Benutzer- und Key-Änderungen und SSH-Vertrauen.

## Steuerung

| Aktion | Bestätigung |
|---|---|
| Starten, Herunterfahren, Neu starten, Pausieren, Fortsetzen, Snapshot erstellen, Backup jetzt | Dialog |
| **Hart stoppen, Snapshot zurücksetzen oder löschen** | Gast-Namen eintippen, das Backend prüft ebenfalls |
| Migration | Vorab-Prüfung (PCI-Passthrough und lokale Ressourcen blockieren). VMs live; laufende Container werden neu gestartet, deshalb Namen eintippen. |
| Bulk (Auswahl oder Tag) | Dialog, beim harten Stopp `stop <Anzahl>` eintippen. Gäste im Zielzustand werden übersprungen. |

**Sicherheitsschalter:**
- `READ_ONLY=true` (Standard): keine Steuerung.
- `DRY_RUN=true`: Aktionen werden geprüft und protokolliert, aber nicht an Proxmox geschickt.
- Ohne Operator-Token bleibt ein Cluster immer lesend.

Jede Aktion wird bis zum Ende des Proxmox-Tasks verfolgt. Das Ergebnis (`ok` oder `error` mit Meldung) steht im Audit-Log, das Task-Log ist aus dem Dashboard heraus abrufbar.

## Node-Verwaltung

Auf der Node-Detailseite unter „Verwaltung“, nur für Admins, mit NodeAdmin-Token:

- **Updates:**
  - Die Liste wird alle 6 h aktualisiert, mit Changelog pro Paket.
  - „Paketlisten aktualisieren“ führt `apt-get update` über die API aus.
  - **Upgrade** läuft per SSH (`apt-get update && apt-get dist-upgrade`, nicht interaktiv) mit Live-Ausgabe.
  - Anschließend prüft das Dashboard, ob ein Neustart nötig ist.
- **Wartungsmodus:**
  - Laufende Gäste werden nacheinander auf den Node mit dem meisten freien RAM migriert.
  - Nicht migrierbare Gäste fährt das Dashboard auf Wunsch sauber herunter.
  - Beim Beenden werden die Gäste zurückgeholt bzw. wieder gestartet.
  - HA-Maintenance gibt es nicht, weil HA bei dir nicht aktiv ist.
- **Neustart:**
  - Vorab-Prüfung: Ist der Node online? Bleibt das Quorum erhalten? Laufen noch Gäste? Ist der Wartungsmodus aktiv?
  - Danach Reboot, und das Dashboard verfolgt, wie der Node offline und wieder online geht.
- **Gast-Updates** (`GUEST_UPDATES=true`, nur Debian und Ubuntu):
  - VMs über den QEMU Guest Agent, Container über `pct exec` per Node-Hilfsskript.
  - Updates prüfen und installieren, Bestätigung mit dem Gast-Namen.

Diese Aktionen laufen als **Jobs** mit Live-Log (SSE). Das Log wird in `/data/jobs/` gespeichert und ist auch nach einem Neustart abrufbar. Pro Node läuft immer nur ein Job.

## Backups, Snapshots, Storage-Prognose

- **Backups:**
  - Die Seite liest die Backup-Jobs (`/cluster/backup`) und die Backup-Volumes aller Backup-Storages (inkl. PBS-Verify-Status) sowie die Snapshots jedes Gasts.
  - Status je Gast: *aktuell*, *zu alt* (älter als `BACKUP_MAX_AGE_HOURS`, Standard 48 h), *kein Backup*, *kein Job* oder *Verify fehlgeschlagen*.
  - Jobs mit „alle Gäste“, Pool, VMID-Liste, Ausschlüssen und Node-Bindung werden korrekt aufgelöst.
- **Storage:**
  - Lineare Regression über 14 Tage Historie ergibt Wachstum pro Tag sowie „90 % in“ und „voll in N Tagen“.
  - Die Prognose ist ein Frühwarnsignal. Einzelne große Sprünge verzerren sie.

## Alerting

Beim ersten Start legt das Dashboard Standardregeln an. Neue Versionen ergänzen neue Standardregeln, von dir gelöschte Regeln kommen dabei nicht zurück.

- **Cluster und Nodes:** Cluster nicht erreichbar, kein Quorum, Node offline, RAM > 90 % (5 min), IO-Delay > 15 % (5 min), Temperatur > 80 °C (2 min)
- **Storage und Datenträger:** Storage > 85 % / > 95 %, Storage voll in < 14 Tagen, SMART-Fehler, SSD-Verschleiß > 80 %
- **Backups und Snapshots:** Backup fehlgeschlagen (auch Sammel-Jobs ohne VMID), kein aktuelles Backup, alter Snapshot > 30 Tage, Gast ohne Backup-Job (standardmäßig aus)
- **Gäste und Wartung:** Gast mit Autostart läuft nicht, Neustart erforderlich

**Kanäle:**
- ntfy (JSON, damit Umlaute erhalten bleiben)
- Gotify
- E-Mail (SMTP mit STARTTLS oder SSL)
- Webhook (JSON, optional HMAC `X-Signature-256`)
- Home Assistant (Webhook-Trigger)

Zugangsdaten werden verschlüsselt gespeichert und in der API nur maskiert angezeigt. Jeder Kanal hat „Test senden“. Alarme lösen sich automatisch auf, „Bestätigen“ stoppt Wiederholungen. Mit `DASHBOARD_URL` enthalten Benachrichtigungen einen Link zum Dashboard.

## API, API-Keys, Prometheus

Die interaktive Doku liegt unter **`/api/docs`**, das OpenAPI-Schema unter `/api/openapi.json`. Alle Endpunkte sind versioniert unter `/api/v1`. Ohne Anmeldung erreichbar sind nur `/api/v1/health`, der Login und die Login-Provider.

**API-Keys** legst du unter Admin → API-Keys an. Der Key wird einmal angezeigt, gespeichert wird nur sein Hash. Keys können ablaufen und lassen sich widerrufen.

| Scope | entspricht | Typisch |
|---|---|---|
| `read` | Viewer | Home Assistant, Prometheus, Grafana, MCP nur lesend |
| `control` | Operator | MCP mit Gast-Steuerung |
| `admin` | Admin | Node-Aktionen, Alarm-Regeln (keine Benutzer- und Key-Verwaltung) |

```bash
KEY=pvd_…
curl -H "Authorization: Bearer $KEY" "https://dash.lan/api/v1/guests?sort=cpu_host&order=desc&limit=5"
curl -H "Authorization: Bearer $KEY" "https://dash.lan/api/v1/analysis/top?metric=cpu&node=pve1&range=24h"
curl -H "Authorization: Bearer $KEY" "https://dash.lan/api/v1/analysis/at?cluster=homelab&node=pve1&at=$(date -d '-2 hours' +%s)&window=300"
```

**Wichtige Endpunkte:**

| Bereich | Pfade |
|---|---|
| Live | `GET /clusters`, `/clusters/{c}`, `/nodes`, `/guests?cluster=&node=&type=&status=&tag=&pool=&q=&sort=&order=&limit=`, `/storage`, `/warnings`, `/stream` (SSE) |
| Historie & Analyse | `GET /history/clusters/{c}[/nodes/{n}\|/guests/{vmid}]?range=`, `/history/storage`, `/analysis/contribution`, `/analysis/at`, `/analysis/top`, `/analysis/allocation` |
| Inventar | `GET /inventory[/clusters/{c}[/nodes/{n}\|/guests/{vmid}]]`, `…/nodes/{n}/smart?disk=`, `/inventory/export?kind=&format=csv\|json`, `POST /inventory/refresh` |
| Backups & Storage | `GET /backups`, `/backups/jobs`, `/storage/forecast`, `POST /backups/refresh` |
| Steuerung | `POST /clusters/{c}/guests/{vmid}/power`, `…/snapshots[/{name}[/rollback]]`, `…/backup`, `…/migrate`, `/actions/bulk`, `GET /actions/capabilities` |
| Tasks & Audit | `GET /tasks`, `/tasks/{c}/{upid}/status\|log`, `/audit` |
| Nodes & Jobs | `GET /node-status`, `…/nodes/{n}/updates[/changelog]`, `POST …/updates/refresh`, `…/upgrade`, `…/maintenance`, `…/reboot`, `GET /jobs[/{id}[/log\|/stream]]` |
| Gast-Updates | `GET /clusters/{c}/guests/{vmid}/os-updates`, `POST …/os-upgrade` |
| Alarme | `GET /alerts`, `POST /alerts/{id}/ack`, CRUD `/alerts/rules`, `/alerts/channels`, `POST /alerts/channels/{id}/test` |
| Konto & Admin | `/auth/login`, `/auth/me`, `/auth/whoami`, `/auth/totp/*`, `/auth/password`, `/auth/sessions`, `/auth/oidc/*`, `/admin/users`, `/admin/api-keys`, `/admin/ssh` |

**Prometheus**, mit einem `read`-Key oder mit `METRICS_PUBLIC=true`:

```yaml
scrape_configs:
  - job_name: proxmox-dashboard
    metrics_path: /metrics
    authorization: { credentials: pvd_… }
    static_configs: [{ targets: ['dash.lan:8080'] }]
```

Exportiert werden:
- `pve_cluster_up`, `pve_cluster_quorate`
- `pve_node_*`: up, info, CPU, RAM, Swap, IO-Delay, Load, Uptime, Wartung, offene Updates
- `pve_guest_*`: up, info mit Tags und Pool, CPU, Host-Anteil, RAM, Disk- und Netzraten, Uptime
- `pve_storage_*`
- `pve_alerts_firing{severity}`

## MCP-Server für KI-Assistenten

`mcp-server/` stellt das Dashboard über das Model Context Protocol bereit (MCP-SDK 2.x, stdio oder Streamable HTTP). Der Server spricht nur mit der Dashboard-API und nutzt dafür einen API-Key. Er sieht und darf also genau das, was dieser Key darf. Jede Aktion steht im Audit-Log als `apikey:<name>`.

**Werkzeuge mit Scope `read`:**
- `cluster_overview`, `list_guests`, `guest_details`
- `top_consumers`, `load_at_time` (Drill-down, z. B. „-2h“)
- `node_status`, `list_alerts`, `recent_tasks`
- `backup_status`, `storage_forecast`

**Zusätzlich mit Scope `control`:**
- `guest_power` (für „stop“ verlangt das Werkzeug den Gast-Namen)
- `create_snapshot`, `start_backup`, `acknowledge_alert`

Werkzeuge, die etwas verändern, sind als `destructive` markiert. Die Server-Instruktionen verlangen, dass der Assistent vor jeder Aktion nachfragt.

**Nicht über MCP verfügbar** sind Node-Neustart, Upgrade, Wartungsmodus, Rollback und das Löschen von Snapshots. Das ist bewusst so.

**Als Docker-Service (Streamable HTTP):**

```bash
# .env: MCP_DASHBOARD_API_KEY=pvd_… (Scope read oder control), MCP_AUTH_TOKEN=$(openssl rand -base64 32)
docker compose --profile mcp up -d
# Endpoint: http://<host>:8765/mcp  mit Header  Authorization: Bearer <MCP_AUTH_TOKEN>
```

**Lokal per stdio,** z. B. für Claude Desktop oder Claude Code:

```json
{
  "mcpServers": {
    "proxmox": {
      "command": "/opt/proxmox-dashboard/mcp-server/.venv/bin/pve-dashboard-mcp",
      "env": { "DASHBOARD_URL": "https://dash.lan", "DASHBOARD_API_KEY": "pvd_…" }
    }
  }
}
```

Installation dafür:

```bash
cd mcp-server && python3 -m venv .venv && .venv/bin/pip install .
```

## Backup und Update des Dashboards

**Was gesichert werden muss:**
- Volume `app-data`: SQLite mit Benutzern, Audit-Log, Regeln und API-Keys, außerdem `secret.key`, das SSH-Schlüsselpaar und die Job-Logs
- `.env` und `certs/`
- Optional die Historie im Volume `vm-data`

```bash
bash deploy/backup.sh                  # → backups/pve-dashboard-<zeit>.tar.gz (Modus 600)
bash deploy/backup.sh --with-history   # + VictoriaMetrics-Snapshot
```

So arbeitet das Skript:
- **Datenbank:** Konsistente Online-Kopie per SQLite-Backup-API, ohne Downtime.
- **Historie:** Über einen VictoriaMetrics-Snapshot.

Das Archiv enthält Geheimnisse (TOTP-Schlüssel, SSH-Private-Key, Tokens). Bewahre es entsprechend auf, z. B. verschlüsselt auf dem PBS oder dem NAS. Für einen täglichen Cronjob:

```cron
30 3 * * * cd /opt/proxmox-dashboard && bash deploy/backup.sh >> backups/backup.log 2>&1 && find backups -name '*.tar.gz' -mtime +30 -delete
```

**Wiederherstellen:**

```bash
bash deploy/restore.sh backups/pve-dashboard-20261024-033000.tar.gz
```

Das Skript stoppt das Dashboard, ersetzt die Daten (und mit Historie im Archiv auch `vm-data`) und startet neu. Eine vorhandene `.env` wird nicht überschrieben.

**Update:**

```bash
cd /opt/proxmox-dashboard
bash deploy/backup.sh                 # immer zuerst
git pull                              # neue Compose-Datei und Skripte
# bei gepinnter Version: DASHBOARD_VERSION in .env anheben
docker compose pull
docker compose up -d
docker compose logs -f dashboard
```

- **Schema-Änderungen:** werden beim Start automatisch und rein additiv angewendet (neue Tabellen und Spalten, keine Datenumschreibung). Für den Rückweg setzt du `DASHBOARD_VERSION` auf die alte Version und spielst das Backup zurück.
- **VictoriaMetrics:** ist im Compose-File auf eine feste Version gepinnt. Aktualisiere sie bewusst und lies dazu die Release Notes.
- **Ändern sich die Node-Skripte** (`deploy/node-setup.sh`, `deploy/pve-setup-roles.sh`), führst du sie erneut aus. Sie sind idempotent.

## Konfiguration (Referenz)

Alle Einstellungen kommen aus der `.env`. Eine vollständige, kommentierte Vorlage ist `.env.example`.

| Variable | Standard | Bedeutung |
|---|---|---|
| `DASHBOARD_VERSION` | `latest` | Image-Tag auf ghcr.io (z. B. `1.0.0`) |
| `PVE_<n>_NAME/URLS/TOKEN_ID/TOKEN_SECRET` | – | Cluster bzw. Host (mehrere URLs = Failover) |
| `PVE_<n>_OPERATOR_TOKEN_*`, `PVE_<n>_NODEADMIN_TOKEN_*` | – | Tokens für Steuerung und Node-Verwaltung |
| `PVE_<n>_VERIFY_TLS`, `PVE_<n>_CA_FILE` | `true`, – | TLS-Prüfung, eigene CA |
| `PVE_<n>_SSH_HOSTS` | Cluster-IPs | `pve1=10.0.0.11,…` |
| `PVE_MOCK` | `false` | Beispieldaten statt Cluster |
| `POLL_INTERVAL`, `NODE_STATUS_INTERVAL` | 10, 30 s | Abfrage-Intervalle |
| `READ_ONLY`, `DRY_RUN` | `true`, `false` | Sicherheitsschalter |
| `VM_URL`, `VM_RETENTION` | Compose-Service, 400d | Historie |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD` | `admin`, zufällig | Erster Admin |
| `TOTP_REQUIRED_ROLES` | `admin,operator` | TOTP-Pflicht |
| `SESSION_MAX_HOURS`, `SESSION_IDLE_MINUTES` | 12, 120 | Sitzungsdauer |
| `COOKIE_SECURE` | `auto` | Secure-Cookie |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | vertrauenswürdige Proxys |
| `DASHBOARD_URL` | – | Links in Benachrichtigungen, OIDC-Callback |
| `METRICS_PUBLIC` | `false` | `/metrics` ohne Key |
| `OIDC_*` | aus | SSO, siehe oben |
| `SSH_USER`, `SSH_PORT` | `pve-dashboard`, 22 | Node-SSH |
| `UPDATE_CHECK_INTERVAL`, `SENSORS_INTERVAL` | 6 h, 60 s | |
| `INVENTORY_INTERVAL` | 900 s | Inventar |
| `BACKUP_MAX_AGE_HOURS`, `BACKUP_REFRESH_INTERVAL` | 48, 900 s | Backups |
| `GUEST_UPDATES` | `false` | apt in Gästen |
| `MCP_DASHBOARD_API_KEY`, `MCP_AUTH_TOKEN`, `MCP_PORT` | – | MCP-Service |

## Entwicklung

```bash
# Backend
cd backend && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker run -d --name vm-dev -p 127.0.0.1:8428:8428 victoriametrics/victoria-metrics:v1.153.0
PVE_MOCK=true READ_ONLY=false DATA_DIR=./data ADMIN_PASSWORD=dev-password-123 TOTP_REQUIRED_ROLES= \
  VM_URL=http://127.0.0.1:8428 STATIC_DIR=../frontend/build .venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest && .venv/bin/ruff check app tests

# Frontend (Node ≥ 22.17, siehe frontend/README.md)
cd frontend && npm install && npm run dev      # Proxy /api → localhost:8000
npm run check && npm run build

# MCP-Server
cd mcp-server && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest

# Eigene Images lokal bauen statt von ghcr.io laden
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

**CI** (`.github/workflows/ci.yml`):
- Jeder Push und jeder Pull Request führt die Backend-Tests, die MCP-Tests sowie Type-Check und Build des Frontends aus.
- Pushes auf `main` und Tags `v*` bauen danach beide Images für amd64 und arm64 und veröffentlichen sie auf ghcr.io. Ein neues Release erstellst du so:

```bash
git tag v1.0.1 && git push origin v1.0.1
```

**Tests:** 120 Backend-Tests mit pytest, gegen den Mock-Cluster, einen gemockten Identity-Provider und VictoriaMetrics, dazu 8 Tests für den MCP-Server.

```
backend/app/
  main.py                 App-Factory, Lifespan (alle Dienste)
  config.py               Einstellungen, Cluster-Liste aus PVE_<n>_*
  cli.py                  Notfall-CLI (Benutzer zurücksetzen)
  pve/                    API-Client (Token, Failover), Mock (Cluster, Aktionen, Inventar, SSH)
  collector/              Polling, Snapshot, Raten, Warnungen
  tsdb/                   VictoriaMetrics: Metriken, Writer, Queries, Backfill
  services/               Zeiträume, Lastanalyse
  inventory/              Hardware/Config-Inventar, Parser, Export
  backups/                Backup-Jobs, -Volumes, Snapshots, Storage-Prognose
  actions/                Gast-Aktionen, Task-Watcher
  nodes/                  Updates, Upgrade, Wartung, Reboot, Sensoren, Gast-Updates
  alerts/                 Metrik-Katalog, Regel-Engine, Benachrichtigungen
  auth/                   Benutzer/Sessions/TOTP, API-Keys, OIDC, Rollen/CSRF
  jobs.py · ssh.py · audit.py · db/ · core/ (Crypto, Rate-Limit, Security-Header)
  api/v1/                 REST-Router · api/prometheus.py
frontend/src/             SvelteKit-SPA (routes/, lib/components/)
mcp-server/               MCP-Server (pve_dashboard_mcp/)
deploy/                   pve-setup-roles.sh · node-setup.sh · backup.sh · restore.sh
.github/workflows/ci.yml  Tests + Multi-Arch-Images nach ghcr.io
```

## Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| „Cluster nicht erreichbar“ | URL und Port 8006 vom Container aus erreichbar? TLS: `PVE_n_CA_FILE` setzen. `docker compose logs dashboard` zeigt den Fehler. |
| Inventar: „Abfrage fehlgeschlagen – fehlen Rechte?“ | Rollen-Skript erneut ausführen (z. B. für `Mapping.Audit`). Prüfen mit `pveum user permissions`. |
| Update-Liste: „Kein NodeAdmin-Token“ | Proxmox verlangt `Sys.Modify` schon zum Auflisten, also `--node-admin` und `PVE_n_NODEADMIN_TOKEN_*` setzen. |
| SSH: „Host-Key nicht bestätigt“ / „passt NICHT“ | Unter Admin → SSH abrufen und vergleichen. Nach einer Neuinstallation den alten Key „vergessen“. |
| Live-Werte frieren hinter dem Proxy ein | SSE wird gepuffert, deshalb `proxy_buffering off` setzen bzw. Buffering für `/api/v1/stream` und `/api/v1/jobs/*/stream` abschalten. |
| Login-Schleife hinter HTTPS | `FORWARDED_ALLOW_IPS` auf die Proxy-IP setzen, damit das Dashboard HTTPS erkennt (`COOKIE_SECURE=auto`). |
| Backup-Übersicht leer | Siehe Hinweis zu `Datastore.AllocateSpace` im Rollen-Abschnitt. |
| Admin-Passwort oder TOTP-Gerät verloren | `docker compose exec dashboard python -m app.cli reset-user admin` gibt ein vorläufiges Passwort aus und setzt TOTP zurück. `… python -m app.cli list-users` listet alle Benutzer. |
