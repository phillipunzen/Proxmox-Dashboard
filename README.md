# Proxmox Dashboard

Selbst gehostetes Dashboard für Proxmox-VE-Cluster mit eigener Monitoring-API.

**Stand: Phase 1 (MVP, nur lesend)**: Cluster-Übersicht, Node- und Gast-Listen mit Live-Werten per SSE, Mock-Modus, REST-API `/api/v1` mit OpenAPI-Doku.

| Phase | Inhalt | Status |
|---|---|---|
| 1 | API-Anbindung, Übersicht, Nodes, Gäste, live, read-only | ✅ |
| 2 | Historie (VictoriaMetrics), Lastanalyse + Drill-down, Hardware-Inventar | – |
| 3 | Steuerung, Bestätigungen, Audit-Log, Login/Rollen | – |
| 4 | Updates, Alerting, API-Keys, Prometheus `/metrics` | – |
| 5 | MCP-Server, Feinschliff | – |

## Architektur

```
Browser / Tools ──► FastAPI (eine Instanz)
                     ├─ /api/v1/*        REST + OpenAPI (/api/docs)
                     ├─ /api/v1/stream   Server-Sent Events
                     ├─ SPA (SvelteKit, statisch)
                     └─ Collector ──► In-Memory-Snapshot
                          │  alle POLL_INTERVAL s: /cluster/resources, /cluster/status
                          │  alle NODE_STATUS_INTERVAL s: /nodes/{node}/status
                          ▼
                     Proxmox VE API (API-Token, Failover über mehrere Node-URLs)
```

Alle Browser und API-Clients lesen denselben Snapshot. Die Last auf Proxmox hängt also nicht davon ab, wie viele Dashboards gleichzeitig offen sind.

**Wichtige Kennzahl „Host-Anteil“:** Proxmox meldet die CPU-Last eines Gasts relativ zu dessen vCPUs. Für die Frage „Woher kommt die Last?“ rechnet das Dashboard das auf die Host-Kapazität um: `cpu × vCPUs / Host-Threads`.

## Schnellstart mit Beispieldaten (Mock)

```bash
cp .env.example .env
sed -i 's/^PVE_MOCK=false/PVE_MOCK=true/' .env
docker compose up -d --build
```

Danach `http://<host>:8080` öffnen. Die API-Doku liegt unter `http://<host>:8080/api/docs`.

Der Mock simuliert den Cluster `homelab` (3 Nodes, 13 VMs, 20 LXC) und einen Standalone-Host `edge`. Die Lastkurven haben einen Tagesverlauf, gelegentliche Spitzen und realistische IO- und Netzwerkraten.

## Einrichtung mit echtem Cluster

### 1. Proxmox-Rollen und Token anlegen

Das Skript auf **einem** Node als root ausführen. Die Konfiguration gilt clusterweit.

```bash
bash deploy/pve-setup-roles.sh
```

Für Phase 1 und 2 reicht der Read-only-Token. Das Skript legt an:

| Rolle | Privilegien | Zweck |
|---|---|---|
| `Dashboard-ReadOnly` | `Sys.Audit, VM.Audit, Datastore.Audit, Pool.Audit, SDN.Audit, VM.GuestAgent.Audit` | Monitoring (Token `dashboard@pve!readonly`) |
| `Dashboard-Operator` (`--operator`) | + `VM.PowerMgmt, VM.Snapshot, VM.Snapshot.Rollback, VM.Backup, VM.Migrate, Datastore.AllocateSpace` | Gast-Steuerung (ab Phase 3) |
| `Dashboard-NodeAdmin` (`--node-admin`) | + `Sys.Modify, Sys.PowerMgmt` | apt-Update, Migrate-all, Node-Reboot (ab Phase 4) |

Die Tokens werden mit `privsep=1` angelegt. Ein Token hat dann nur die Rechte seiner eigenen ACL, auch wenn der Benutzer mehr darf. Hinweis zu PVE 9: `VM.Monitor` gibt es nicht mehr, für Guest-Agent-Infos ist jetzt `VM.GuestAgent.Audit` zuständig.

Rechte prüfen:

```bash
pveum user permissions 'dashboard@pve!readonly' --path /
```

### 2. `.env` ausfüllen

```bash
cp .env.example .env
```

Dann `PVE_1_URLS`, `PVE_1_TOKEN_ID` und `PVE_1_TOKEN_SECRET` setzen. Weitere Cluster oder Standalone-Hosts kommen als `PVE_2_*`, `PVE_3_*` usw. dazu.

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

## API (Auszug)

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/v1/meta` | Version, Mock-/Read-only-Status, Poll-Intervall |
| GET | `/api/v1/health` | `ok` / `degraded` je Cluster |
| GET | `/api/v1/clusters` | Alle Cluster mit Nodes, Summen, Warnungen |
| GET | `/api/v1/clusters/{cluster}` | Vollständiger Snapshot inkl. Gäste und Storages |
| GET | `/api/v1/nodes?cluster=` | Nodes |
| GET | `/api/v1/clusters/{cluster}/nodes/{node}` | Ein Node |
| GET | `/api/v1/guests?cluster=&node=&type=&status=&tag=&pool=&q=&sort=&order=&limit=` | Gäste filtern und sortieren (z. B. Top 5 nach `cpu_host`) |
| GET | `/api/v1/clusters/{cluster}/guests/{vmid}` | Ein Gast |
| GET | `/api/v1/storage?cluster=&node=` | Storages |
| GET | `/api/v1/warnings` | Aktuelle Warnungen |
| GET | `/api/v1/stream` | SSE: `event: snapshot` nach jedem Poll |

Beispiel: Top 5 Gäste nach Host-CPU-Anteil:

```bash
curl -s 'http://localhost:8080/api/v1/guests?sort=cpu_host&order=desc&limit=5' | jq '.[] | {vmid, name, node, cpu_host}'
```

> In Phase 1 ist die API **nicht authentifiziert** und nur lesend. Betreibe das Dashboard deshalb vorerst nur im LAN. API-Keys mit Scopes kommen in Phase 4, der Login in Phase 3.

## Entwicklung

**Backend** (Python ≥ 3.12):

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
PVE_MOCK=true STATIC_DIR=../frontend/build .venv/bin/uvicorn app.main:app --reload
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
  api/v1/routes.py      REST + SSE
  core/web.py           Security-Header, CSP, Auslieferung der SPA
frontend/src/
  lib/live.svelte.ts    SSE-Client + Live-Historie im Browser
  routes/               Übersicht, Nodes, Node-Detail, Gäste
deploy/pve-setup-roles.sh
```
