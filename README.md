# Proxmox Dashboard

Selbst gehostetes Dashboard für Proxmox-VE-Cluster mit eigener Monitoring-API.

**Stand: Phase 2 (nur lesend)**: Live-Übersicht, Historie bis 400 Tage (VictoriaMetrics), Lastanalyse mit Drill-down, Hardware-Inventar mit Export, Mock-Modus, REST-API `/api/v1` mit OpenAPI-Doku.

| Phase | Inhalt | Status |
|---|---|---|
| 1 | API-Anbindung, Übersicht, Nodes, Gäste, live, read-only | ✅ |
| 2 | Historie (VictoriaMetrics), Lastanalyse + Drill-down, Hardware-Inventar | ✅ |
| 3 | Steuerung, Bestätigungen, Audit-Log, Login/Rollen | – |
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

Der Mock simuliert den Cluster `homelab` (3 Nodes, 13 VMs, 20 LXC) und einen Standalone-Host `edge`. Die Lastkurven haben einen Tagesverlauf, gelegentliche Spitzen und realistische IO- und Netzwerkraten. Beim ersten Start schreibt er 30 Tage Historie in eine leere VictoriaMetrics-Instanz (`MOCK_BACKFILL=true`, ca. 1,5 Mio. Werte in rund 5 s). Dazu kommt ein Inventar mit NVMe-, SSD- und HDD-Laufwerken (eine SSD mit SMART-Fehler), Bond, Bridges, VLAN, GPU-Passthrough per PCI-Mapping und HBA-Passthrough.

> Wechsel von Mock auf einen echten Cluster: Lösche vorher die Mock-Historie mit `docker compose down -v`. Das entfernt das Volume `vm-data`.

## Einrichtung mit echtem Cluster

### 1. Proxmox-Rollen und Token anlegen

Das Skript auf **einem** Node als root ausführen. Die Konfiguration gilt clusterweit.

```bash
bash deploy/pve-setup-roles.sh
```

Für Phase 1 und 2 reicht der Read-only-Token. Das Skript legt an:

| Rolle | Privilegien | Zweck |
|---|---|---|
| `Dashboard-ReadOnly` | `Sys.Audit, VM.Audit, Datastore.Audit, Pool.Audit, SDN.Audit, Mapping.Audit, VM.GuestAgent.Audit` | Monitoring und Inventar (Token `dashboard@pve!readonly`) |
| `Dashboard-Operator` (`--operator`) | + `VM.PowerMgmt, VM.Snapshot, VM.Snapshot.Rollback, VM.Backup, VM.Migrate, Datastore.AllocateSpace` | Gast-Steuerung (ab Phase 3) |
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

Die vollständige, interaktive Doku liegt unter `/api/docs`.

| Methode | Pfad | Beschreibung |
|---|---|---|
| GET | `/api/v1/meta` | Version, Mock-/Read-only-Status, ob Historie aktiv ist |
| GET | `/api/v1/health` | `ok` / `degraded` je Cluster |
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

Beispiele:

```bash
# Wer hat in den letzten 24 h auf pve1 die meiste CPU verbraucht?
curl -s 'http://localhost:8080/api/v1/analysis/top?metric=cpu&node=pve1&range=24h&limit=5' | jq '.[] | {name, avg, p95, max}'

# Was lief am 24.10. um 10:15 Uhr auf pve1?
curl -s "http://localhost:8080/api/v1/analysis/at?cluster=homelab&node=pve1&at=$(date -d '2026-10-24 10:15' +%s)&window=300" | jq '.guests[:5]'
```

> Bis einschließlich Phase 2 ist die API **nicht authentifiziert** und nur lesend. Betreibe das Dashboard deshalb vorerst nur im LAN. Der Login kommt in Phase 3, API-Keys mit Scopes in Phase 4.

## Entwicklung

**Backend** (Python ≥ 3.12):

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
docker run -d --name vm-dev -p 127.0.0.1:8428:8428 victoriametrics/victoria-metrics:v1.153.0
PVE_MOCK=true VM_URL=http://127.0.0.1:8428 STATIC_DIR=../frontend/build .venv/bin/uvicorn app.main:app --reload
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
  core/web.py           Security-Header, CSP, Auslieferung der SPA
frontend/src/
  lib/live.svelte.ts    SSE-Client + Live-Historie im Browser
  routes/               Übersicht, Nodes, Node-Detail, Gäste, Gast-Detail, Lastanalyse, Inventar
deploy/pve-setup-roles.sh
```
