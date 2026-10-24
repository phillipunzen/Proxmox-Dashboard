# Proxmox Dashboard

Selbst gehostetes Dashboard für Proxmox-VE-Cluster mit eigener Monitoring-API.

**Stand: Phase 3**: Live-Übersicht, Historie bis 400 Tage (VictoriaMetrics), Lastanalyse mit Drill-down, Hardware-Inventar mit Export, **Login mit Argon2 + TOTP, Rollen, Gast-Steuerung mit Bestätigungen, Proxmox-Aufgaben, Audit-Log**, Mock-Modus, REST-API `/api/v1` mit OpenAPI-Doku.

| Phase | Inhalt | Status |
|---|---|---|
| 1 | API-Anbindung, Übersicht, Nodes, Gäste, live, read-only | ✅ |
| 2 | Historie (VictoriaMetrics), Lastanalyse + Drill-down, Hardware-Inventar | ✅ |
| 3 | Steuerung, Bestätigungen, Audit-Log, Login/Rollen | ✅ |
| 4 | Updates, Alerting, API-Keys, Prometheus `/metrics` | – |
| 5 | MCP-Server, Feinschliff | – |

## Architektur

```
Browser / Tools ──► FastAPI (eine Instanz)
                     ├─ /api/v1/*        REST + OpenAPI (/api/docs)
                     ├─ /api/v1/stream   Server-Sent Events
                     ├─ SPA (SvelteKit, statisch)
                     ├─ Collector ──► In-Memory-Snapshot ──► VictoriaMetrics (Historie)
                     │    alle POLL_INTERVAL s: /cluster/resources, /cluster/status
                     │    alle NODE_STATUS_INTERVAL s: /nodes/{node}/status
                     └─ Inventar (alle INVENTORY_INTERVAL s)
                          disks/list, network, hardware/pci, Gast-Configs, Guest Agent
                          ▼
                     Proxmox VE API (API-Token, Failover über mehrere Node-URLs)
```

Alle Browser und API-Clients lesen denselben Snapshot. Die Last auf Proxmox hängt also nicht davon ab, wie viele Dashboards gleichzeitig offen sind. Schreibvorgänge in die Zeitreihen-Datenbank laufen gepuffert im Hintergrund: Fällt VictoriaMetrics kurz aus, wird nachgeliefert, und das Polling verzögert sich nicht.

**Wichtige Kennzahl „Host-Anteil“:** Proxmox meldet die CPU-Last eines Gasts relativ zu dessen vCPUs. Für die Frage „Woher kommt die Last?“ rechnet das Dashboard das auf belegte Host-Threads um: `cpu × vCPUs`. Ein Gast mit 50 % auf 4 vCPUs belegt also 2 Threads. In der Lastanalyse werden diese Werte gestapelt und mit „Host gesamt“ und der Kapazität verglichen. Die Lücke zwischen den Gästen und dem Host-Wert ist Last des Hosts selbst (ZFS, Backups, Kernel).

### Gespeicherte Zeitreihen

Die Werte werden pro Poll als Gauges geschrieben, standardmäßig alle 10 s:

| Metrik | Labels |
|---|---|
| `pve_cluster_{cpu_ratio,cpus,mem_used_bytes,mem_total_bytes,netin_bps,netout_bps,diskread_bps,diskwrite_bps}` | cluster |
| `pve_node_{up,cpu_ratio,cpus,cpu_cores_used,mem_used_bytes,mem_total_bytes,swap_used_bytes,iowait_ratio,load1}` | cluster, node |
| `pve_guest_{cpu_cores,cpu_ratio,mem_used_bytes,diskread_bps,diskwrite_bps,netin_bps,netout_bps}` | cluster, node, vmid, name, type |
| `pve_storage_{used,total}_bytes` | cluster, node (`shared` bei gemeinsamen Storages), storage, type |

Gast-Zeitreihen tragen das Node-Label. Deshalb bleibt die Frage „wer hat *diesen* Node zum Zeitpunkt t belastet“ auch nach Migrationen korrekt. Speicherbedarf: Bei ~35 Gästen fallen rund 1–2 GB pro Jahr an. Die Aufbewahrung wird über `VM_RETENTION` eingestellt (Standard 400 Tage).

## Schnellstart mit Beispieldaten (Mock)

```bash
cp .env.example .env
sed -i 's/^PVE_MOCK=false/PVE_MOCK=true/' .env
docker compose up -d --build
```

Danach `http://<host>:8080` öffnen. Die API-Doku liegt unter `http://<host>:8080/api/docs`.

**Erste Anmeldung:** Beim ersten Start legt das Dashboard den Benutzer `admin` an und schreibt ein zufälliges Startpasswort einmalig ins Log:

```bash
docker compose logs dashboard | grep "Initial admin"
```

Nach dem Login führt das Dashboard dich durch zwei Schritte: Zwei-Faktor-Anmeldung einrichten (für Admins und Operatoren Pflicht) und ein eigenes Passwort festlegen. Alternativ setzt du `ADMIN_PASSWORD` in der `.env` **vor** dem ersten Start.

Der Mock simuliert den Cluster `homelab` (3 Nodes, 13 VMs, 20 LXC) und einen Standalone-Host `edge`. Die Lastkurven haben einen Tagesverlauf, gelegentliche Spitzen und realistische IO- und Netzwerkraten. Beim ersten Start schreibt er 30 Tage Historie in eine leere VictoriaMetrics-Instanz (`MOCK_BACKFILL=true`, ca. 1,5 Mio. Werte in rund 5 s). Dazu kommt ein Inventar mit NVMe-, SSD- und HDD-Laufwerken (eine SSD mit SMART-Fehler), Bond, Bridges, VLAN, GPU-Passthrough per PCI-Mapping und HBA-Passthrough.

> Wechsel von Mock auf einen echten Cluster: Lösche die Mock-Historie mit `docker compose down && docker volume rm proxmox-dashboard_vm-data`. Das Volume `app-data` mit Benutzern und Audit-Log bleibt dabei erhalten. `docker compose down -v` würde beide löschen.

## Einrichtung mit echtem Cluster

### 1. Proxmox-Rollen und Token anlegen

Das Skript auf **einem** Node als root ausführen. Die Konfiguration gilt clusterweit.

```bash
bash deploy/pve-setup-roles.sh
```

Für reines Monitoring reicht der Read-only-Token. Für die Steuerung (Start/Stopp, Snapshots, Backup, Migration) brauchst du zusätzlich den Operator-Token:

```bash
bash deploy/pve-setup-roles.sh --operator
```

Das Skript legt an:

| Rolle | Privilegien | Zweck |
|---|---|---|
| `Dashboard-ReadOnly` | `Sys.Audit, VM.Audit, Datastore.Audit, Pool.Audit, SDN.Audit, Mapping.Audit, VM.GuestAgent.Audit` | Monitoring und Inventar (Token `dashboard@pve!readonly`) |
| `Dashboard-Operator` (`--operator`) | + `VM.PowerMgmt, VM.Snapshot, VM.Snapshot.Rollback, VM.Backup, VM.Migrate, Datastore.AllocateSpace` | Gast-Steuerung (Token `dashboard@pve!operator`) |
| `Dashboard-NodeAdmin` (`--node-admin`) | + `Sys.Modify, Sys.PowerMgmt` | apt-Update, Migrate-all, Node-Reboot (ab Phase 4) |

Wofür welches Recht gebraucht wird:

- `Sys.Audit`: Node-Status, Datenträger und SMART, Netzwerk, PCI
- `VM.Audit`: Gast-Konfigurationen
- `VM.GuestAgent.Audit`: Betriebssystem und IPs über den QEMU Guest Agent
- `Mapping.Audit`: PCI-Mappings auflösen

Fehlt ein Recht, bleibt das betroffene Feld leer, und das Inventar zeigt „Abfrage fehlgeschlagen“. Hast du die Rollen schon in Phase 1 angelegt, führe das Skript einfach erneut aus. Es aktualisiert die Rolle um `Mapping.Audit`.

Die Tokens werden mit `privsep=1` angelegt. Ein Token hat dann nur die Rechte seiner eigenen ACL, auch wenn der Benutzer mehr darf. Hinweis zu PVE 9: `VM.Monitor` gibt es nicht mehr, für Guest-Agent-Infos ist jetzt `VM.GuestAgent.Audit` zuständig.

Rechte prüfen:

```bash
pveum user permissions 'dashboard@pve!readonly' --path /
```

### 2. `.env` ausfüllen

```bash
cp .env.example .env
```

Dann `PVE_1_URLS`, `PVE_1_TOKEN_ID` und `PVE_1_TOKEN_SECRET` setzen. Für die Steuerung kommen `PVE_1_OPERATOR_TOKEN_ID`/`_SECRET` dazu sowie `READ_ONLY=false`. Weitere Cluster oder Standalone-Hosts kommen als `PVE_2_*`, `PVE_3_*` usw. dazu.

**Selbstsignierte Zertifikate:** `PVE_1_VERIFY_TLS=true` lassen und stattdessen die Cluster-CA einbinden:

```bash
scp root@pve1:/etc/pve/pve-root-ca.pem certs/
```

In `.env` dann `PVE_1_CA_FILE=/certs/pve-root-ca.pem` eintragen. `PVE_1_VERIFY_TLS=false` ist nur als Notlösung gedacht.

### 3. Starten

```bash
docker compose up -d --build
docker compose logs -f
```

## Anmeldung, Rollen und Sicherheit

| Rolle | Darf |
|---|---|
| **Viewer** | Alles sehen: Übersicht, Analyse, Inventar, Aufgaben, eigene Audit-Einträge |
| **Operator** | zusätzlich Gäste starten/herunterfahren/neu starten/pausieren/stoppen, Snapshots, Backup, Migration, Bulk-Aktionen |
| **Admin** | zusätzlich Benutzerverwaltung (anlegen, Rolle, deaktivieren, Passwort/TOTP zurücksetzen) und das komplette Audit-Log |

- **Passwörter:** Gespeichert als Argon2id-Hash, mindestens 12 Zeichen. Neue oder zurückgesetzte Benutzer bekommen ein vorläufiges Passwort und müssen es beim ersten Login ändern.
- **TOTP (RFC 6238):** Kompatibel mit jeder Authenticator-App. Ein Code kann nicht zweimal verwendet werden. Das Geheimnis wird verschlüsselt gespeichert (Schlüssel: `APP_SECRET_KEY` bzw. `/data/secret.key`). Für welche Rollen TOTP Pflicht ist, legt `TOTP_REQUIRED_ROLES` fest (Standard: `admin,operator`). Verliert jemand sein Gerät, setzt ein Admin TOTP unter „Benutzer“ zurück.
- **Sitzungen:** Serverseitig gespeichert, im Cookie steht nur ein Zufallstoken. Das Cookie ist `HttpOnly` und `SameSite=Strict`, hinter HTTPS zusätzlich `Secure`. Standardmäßig gilt eine Sitzung höchstens 12 h und endet nach 2 h Inaktivität. Ändert jemand sein Passwort, deaktiviert ein Admin ihn oder wird TOTP aktiviert, enden die anderen Sitzungen des Benutzers.
- **Brute-Force-Schutz:** Höchstens 20 Login-Versuche pro IP und 10 pro Benutzername in 5 Minuten. Nach 5 Fehlversuchen ist das Konto 15 Minuten gesperrt. Die Fehlermeldung verrät nicht, ob ein Benutzer existiert.
- **CSRF:** Jede schreibende Anfrage braucht den Header `X-CSRF-Token` passend zur Sitzung. Zusätzlich wird der `Origin`-Header geprüft.
- **Header:** CSP mit Hash für das einzige Inline-Script, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`.
- **Hinter einem Reverse-Proxy:** Trage seine IP in `FORWARDED_ALLOW_IPS` ein. Dann landen die echten Client-IPs im Audit-Log und im Rate-Limit.

## Steuerung

| Aktion | Bestätigung |
|---|---|
| Starten, Herunterfahren (ACPI/Agent), Neu starten, Pausieren, Fortsetzen | Dialog |
| **Hart stoppen** | Gast-Namen eintippen |
| Snapshot erstellen (optional mit RAM) | Dialog |
| **Snapshot zurücksetzen / löschen** | Gast-Namen eintippen |
| Backup jetzt (vzdump auf ein Backup-Storage des Nodes, Snapshot-/Suspend-/Stop-Modus) | Dialog |
| Migration (VM live, mit lokalen Disks; vorher Prüfung auf blockierende Ressourcen wie PCI-Passthrough) | Dialog. Laufende Container werden dabei neu gestartet, deshalb Gast-Namen eintippen |
| Bulk: Start/Herunterfahren/Neustart/Stopp für eine Auswahl oder alle Gäste mit einem Tag | Dialog, beim Stopp `stop <Anzahl>` eintippen |
| Konsole | Link in die Proxmox-Oberfläche (noVNC / xterm.js) |

Das Backend prüft die Bestätigung ebenfalls. API-Clients müssen also beim harten Stopp auch `confirm=<Gast-Name>` mitschicken.

Jede Aktion landet im **Audit-Log**: wer, wann, von welcher IP, mit welchen Parametern. Das Dashboard verfolgt den Proxmox-Task bis zum Ende und trägt das Ergebnis nach, also `ok` oder `error` samt Fehlermeldung. Das Task-Log lässt sich direkt öffnen. Auf der Seite **Aufgaben** stehen alle Proxmox-Tasks des Clusters, auch die von anderen, z. B. nächtliche Backups.

**Sicherheitsschalter in der `.env`:**

- `READ_ONLY=true` (Standard): keinerlei Steuerung.
- `DRY_RUN=true`: Aktionen werden geprüft und protokolliert, aber nicht an Proxmox geschickt. Gut zum Ausprobieren.
- Ohne Operator-Token bleibt der jeweilige Cluster lesend, auch wenn `READ_ONLY=false` gesetzt ist.

Node-Aktionen kommen in Phase 4: Updates, Wartungsmodus mit Migrate-All, Reboot mit Vorab-Check.

## API (Auszug)

Die vollständige, interaktive Doku liegt unter `/api/docs`.

| Methode | Pfad | Beschreibung |
|---|---|---|
| POST | `/api/v1/auth/login` | Anmelden (`{username, password}`), setzt das Session-Cookie. Antwort `status`: `ok`, `totp`, `totp_setup` oder `password_change` |
| POST | `/api/v1/auth/totp/verify` | Zweiter Faktor (`{code}`) |
| GET | `/api/v1/auth/me` | Aktueller Benutzer inkl. `csrf_token` |
| POST | `/api/v1/auth/logout`, `/api/v1/auth/password`, `/api/v1/auth/totp/{setup,enable,disable}` | Konto |
| GET/DELETE | `/api/v1/auth/sessions[/{id}]` | Eigene Sitzungen |
| GET/POST/PATCH/DELETE | `/api/v1/admin/users[/{id}]`, `…/reset-password`, `…/reset-totp` | Benutzerverwaltung (Admin) |
| GET | `/api/v1/health` | `ok` / `degraded`, ohne Anmeldung (für Healthchecks) |
| GET | `/api/v1/meta` | Version, Mock-/Read-only-/Dry-Run-Status, ob Historie aktiv ist |
| GET | `/api/v1/clusters` | Alle Cluster mit Nodes, Summen, Warnungen |
| GET | `/api/v1/clusters/{cluster}` | Vollständiger Snapshot inkl. Gäste und Storages |
| GET | `/api/v1/nodes`, `/api/v1/clusters/{c}/nodes/{node}` | Nodes |
| GET | `/api/v1/guests?cluster=&node=&type=&status=&tag=&pool=&q=&sort=&order=&limit=` | Gäste filtern und sortieren |
| GET | `/api/v1/clusters/{c}/guests/{vmid}` | Ein Gast |
| GET | `/api/v1/storage`, `/api/v1/warnings` | Storages, Warnungen |
| GET | `/api/v1/stream` | SSE: `event: snapshot` nach jedem Poll |
| GET | `/api/v1/history/clusters/{c}[/nodes/{node}\|/guests/{vmid}]?range=1h\|6h\|24h\|7d\|30d` | Verlauf (oder `start`/`end` in Unix-Sekunden) |
| GET | `/api/v1/history/storage?cluster=&range=` | Storage-Belegung über die Zeit |
| GET | `/api/v1/analysis/contribution?cluster=&node=&metric=cpu\|mem\|diskread\|diskwrite\|netin\|netout&range=&top=` | Host-Last aufgeteilt in die Top-N-Gäste und den Rest |
| GET | `/api/v1/analysis/at?cluster=&node=&metric=&at=&window=` | Drill-down: Ranking der Gäste zu einem Zeitpunkt |
| GET | `/api/v1/analysis/top?metric=&range=&limit=` (+ Filter) | Top-Verbraucher mit Ø, p95, Max |
| GET | `/api/v1/analysis/allocation` | vCPU und RAM: zugewiesen vs. physisch je Node |
| GET | `/api/v1/inventory[/clusters/{c}[/nodes/{n}\|/guests/{vmid}]]` | Hardware- und Konfigurations-Inventar |
| GET | `/api/v1/inventory/clusters/{c}/nodes/{n}/smart?disk=/dev/sda` | SMART-Details (live) |
| POST | `/api/v1/inventory/refresh` | Inventar sofort neu erfassen (höchstens alle 30 s) |
| GET | `/api/v1/inventory/export?kind=nodes\|disks\|network\|pci\|guests\|guest_disks\|guest_nics&format=csv\|json` | Export (CSV mit `;` und UTF-8-BOM, öffnet direkt in Excel) |
| GET | `/api/v1/actions/capabilities` | Ob Steuerung je Cluster möglich ist und warum nicht |
| POST | `/api/v1/clusters/{c}/guests/{vmid}/power` | `{action: start\|shutdown\|stop\|reboot\|suspend\|resume, confirm?, timeout?}` |
| GET/POST | `/api/v1/clusters/{c}/guests/{vmid}/snapshots` | Snapshots auflisten / erstellen |
| POST/DELETE | `/api/v1/clusters/{c}/guests/{vmid}/snapshots/{name}[/rollback]` | Zurücksetzen / löschen (mit `confirm`) |
| GET/POST | `/api/v1/clusters/{c}/guests/{vmid}/backup[-storages]` | Backup-Ziele / Backup starten |
| GET/POST | `/api/v1/clusters/{c}/guests/{vmid}/migrate` | Prüfung / Migration |
| POST | `/api/v1/actions/bulk` | `{cluster, action, vmids \| tag, confirm?}` |
| GET | `/api/v1/tasks?cluster=&vmid=&errors_only=` | Proxmox-Tasks |
| GET | `/api/v1/tasks/{cluster}/{upid}/status\|log` | Task-Status und -Log |
| GET | `/api/v1/audit?action=&status=&actor_name=&vmid=&before_id=` | Audit-Log |

**Skripte und Tools:** Bis API-Keys kommen (Phase 4), meldest du dich wie der Browser an. Speichere das Cookie und schicke bei schreibenden Aufrufen den `X-CSRF-Token` mit:

```bash
curl -c jar -H 'Content-Type: application/json' -d '{"username":"viewer1","password":"…"}' http://localhost:8080/api/v1/auth/login
curl -b jar http://localhost:8080/api/v1/guests?sort=cpu_host\&order=desc\&limit=5
```

Beispiele:

```bash
# Wer hat in den letzten 24 h auf pve1 die meiste CPU verbraucht? (Cookie aus dem Login)
curl -s -b jar 'http://localhost:8080/api/v1/analysis/top?metric=cpu&node=pve1&range=24h&limit=5' | jq '.[] | {name, avg, p95, max}'

# Was lief am 24.10. um 10:15 Uhr auf pve1?
curl -s -b jar "http://localhost:8080/api/v1/analysis/at?cluster=homelab&node=pve1&at=$(date -d '2026-10-24 10:15' +%s)&window=300" | jq '.guests[:5]'
```

> Alle Endpunkte außer `/api/v1/health` und dem Login verlangen eine Anmeldung.

## Entwicklung

**Backend** (Python ≥ 3.12):

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker run -d --name vm-dev -p 127.0.0.1:8428:8428 victoriametrics/victoria-metrics:v1.153.0
PVE_MOCK=true READ_ONLY=false DATA_DIR=./data ADMIN_PASSWORD=dev-password-123 TOTP_REQUIRED_ROLES= \
  VM_URL=http://127.0.0.1:8428 STATIC_DIR=../frontend/build .venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest          # Tests
.venv/bin/ruff check app tests
```

**Frontend:** Benötigt Node ≥ 22.17, siehe [frontend/README.md](frontend/README.md). Der Dev-Server leitet `/api` an das Backend auf Port 8000 weiter.

## Projektstruktur

```
backend/app/
  config.py             Einstellungen + Cluster-Liste aus PVE_<n>_*
  pve/client.py         Async-PVE-Client (Token, TLS, Failover)
  pve/mock.py           Synthetischer Cluster
  collector/builder.py  Rohdaten → Snapshot (Raten, Summen, Warnungen) – reine Funktionen
  collector/poller.py   Polling-Schleifen pro Cluster
  collector/state.py    Snapshot-Store + Änderungs-Benachrichtigung für SSE
  tsdb/                 VictoriaMetrics: Metriken, gepufferter Writer, Queries, Mock-Backfill
  services/history.py   Zeiträume, Schrittweiten, Selektoren
  services/analysis.py  Lastanalyse: Anteile, Drill-down, Top, Overcommitment
  inventory/            Hardware-/Config-Inventar: Collector, Parser, Export
  pve/mock_inventory.py Mock-Daten für Disks, NICs, PCI, Gast-Configs, Guest Agent
  api/v1/routes.py      REST + SSE (Live)
  api/v1/history.py     Historie + Lastanalyse
  api/v1/inventory.py   Inventar + Export
  api/v1/auth.py        Login, TOTP, Konto, Benutzerverwaltung
  api/v1/actions.py     Steuerung, Tasks, Audit-Log
  auth/                 Benutzer/Sessions/TOTP (service.py), Rollen + CSRF (deps.py)
  actions/service.py    Aktionen mit Sicherheitsprüfungen + TaskWatcher
  audit.py              Audit-Log
  db/                   SQLite (SQLAlchemy async): users, sessions, audit_log
  core/crypto.py        Argon2id, Fernet-Verschlüsselung, Tokens
  pve/mock_actions.py   Mock: Tasks, Statuswechsel, Snapshots, Migration
  core/web.py           Security-Header, CSP, Auslieferung der SPA
frontend/src/
  lib/live.svelte.ts    SSE-Client + Live-Historie im Browser
  lib/auth.svelte.ts    Sitzung, Rollen, Fähigkeiten
  routes/               Übersicht, Nodes, Gäste (+ Bulk), Gast-Detail (+ Steuerung), Lastanalyse,
                        Inventar, Aufgaben, Audit-Log, Benutzer, Konto, Login
deploy/pve-setup-roles.sh
```
