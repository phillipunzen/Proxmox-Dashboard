# Proxmox Dashboard

Selbst gehostetes Dashboard für Proxmox-VE-Cluster mit eigener Monitoring-API.

**Stand: Phase 4**: Live-Übersicht, Historie bis 400 Tage (VictoriaMetrics), Lastanalyse mit Drill-down, Hardware-Inventar mit Export, Login mit Argon2 + TOTP, Rollen, Gast-Steuerung, Audit-Log, **Node-Updates per SSH mit Live-Ausgabe, Wartungsmodus, Node-Neustart mit Vorab-Prüfung, Alerting (ntfy, Gotify, E-Mail, Webhook, Home Assistant), API-Keys mit Scopes, Prometheus-Endpunkt**, Mock-Modus, REST-API `/api/v1` mit OpenAPI-Doku.

| Phase | Inhalt | Status |
|---|---|---|
| 1 | API-Anbindung, Übersicht, Nodes, Gäste, live, read-only | ✅ |
| 2 | Historie (VictoriaMetrics), Lastanalyse + Drill-down, Hardware-Inventar | ✅ |
| 3 | Steuerung, Bestätigungen, Audit-Log, Login/Rollen | ✅ |
| 4 | Updates, Alerting, API-Keys, Prometheus `/metrics` | ✅ |
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
bash deploy/pve-setup-roles.sh   Proxmox-Rollen und -Tokens
deploy/node-setup.sh        SSH-Zugang auf den Nodes (eingeschränkt)
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
| `Dashboard-NodeAdmin` (`--node-admin`) | + `Sys.Modify, Sys.PowerMgmt` | Update-Liste, Paketlisten aktualisieren, Changelog, Node-Neustart (Token `dashboard@pve!nodeadmin`) |

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

## Node-Verwaltung (Admin)

Auf der Node-Detailseite unter **Verwaltung**:

| Funktion | Wie | Voraussetzung |
|---|---|---|
| Verfügbare Updates mit Changelog | `GET /nodes/{n}/apt/update` alle 6 h | NodeAdmin-Token: Proxmox verlangt `Sys.Modify` schon zum *Auflisten* |
| Paketlisten aktualisieren | `apt-get update` über die API (Task) | NodeAdmin-Token |
| **Upgrade installieren** | `apt-get dist-upgrade` per SSH, Ausgabe live im Browser | SSH eingerichtet, Node-Namen eintippen |
| Neustart erforderlich? | Prüft `/var/run/reboot-required` und vergleicht laufenden mit neuestem Kernel | SSH |
| **Wartungsmodus** | Laufende Gäste werden nacheinander auf den Node mit dem meisten freien RAM migriert (VMs live, Container per Neustart). Nicht migrierbare Gäste (z. B. mit PCI-Passthrough) werden auf Wunsch sauber heruntergefahren. Beim Beenden werden sie zurückgeholt bzw. wieder gestartet. | Operator- und NodeAdmin-Token |
| **Neustart** | Vorab-Prüfung: Node online? Bleibt das Quorum erhalten? Laufen noch Gäste? Wartungsmodus aktiv? Danach Reboot. Das Dashboard verfolgt, wie der Node offline und wieder online geht. | NodeAdmin-Token, Node-Namen eintippen |
| Mainboard, Temperaturen | DMI aus `/sys/class/dmi/id`, `sensors -j` (lm-sensors) | SSH |

Diese Aktionen laufen als **Jobs** mit eigenem Log. Das Log lässt sich jederzeit wieder öffnen, auch nach einem Neustart des Dashboards, und wird zusätzlich als Datei in `/data/jobs/` gespeichert. Pro Node läuft immer nur ein Job gleichzeitig.

HA ist bei dir nicht aktiv. Der Wartungsmodus ist deshalb ein Dashboard-Status samt Evakuierung und nicht der HA-Maintenance-Modus von Proxmox.

### SSH einrichten

1. Den Public Key kopierst du unter **Admin → SSH**. Das Dashboard erzeugt das Schlüsselpaar beim ersten Start in `/data/ssh/`.
2. Auf **jedem Node** als root ausführen:
   ```bash
   bash deploy/node-setup.sh "ssh-ed25519 AAAA… pve-dashboard"
   ```
   Das Skript legt den Benutzer `pve-dashboard` an, nur mit Key-Login und ohne Passwort. Der Key ist auf ein festes Dispatcher-Skript beschränkt (`restrict,command=…`), das nur `upgrade`, `update`, `dmi`, `sensors`, `reboot-required` und `version` annimmt. Dazu kommt **eine** sudo-Regel für ein root-eigenes Hilfsskript. Eine Shell, Forwarding oder beliebige Befehle sind nicht möglich.
3. Unter **Admin → SSH** den Host-Key jedes Nodes abrufen und mit dem Fingerprint vergleichen, den das Skript ausgibt. Erst dann vertrauen. Das Dashboard verbindet sich nur zu bestätigten Host-Keys.

> Ehrlicher Hinweis: `upgrade` führt apt als root aus, und Paket-Skripte laufen als root. Wer den privaten Schlüssel des Dashboards besitzt, kann damit Updates aus deinen konfigurierten Repositories installieren, aber keine beliebigen Befehle ausführen. Sichere das Volume `app-data` entsprechend.

## Alerting

Unter **Alarme** stehen aktive Alarme, der Verlauf, die Regeln und die Benachrichtigungskanäle.

Beim ersten Start legt das Dashboard sinnvolle Regeln an:

- Cluster nicht erreichbar, kein Quorum, Node offline
- RAM > 90 % für 5 min, IO-Delay > 15 % für 5 min, Temperatur > 80 °C für 2 min
- Storage > 85 % / > 95 %
- SMART-Fehler, SSD-Verschleiß > 80 %
- Backup fehlgeschlagen (letzte 26 h, auch für Sammel-Jobs ohne VMID)
- Gast mit Autostart läuft nicht, Neustart erforderlich

**Regeln:** Metrik, Vergleich und Schwellwert, Mindestdauer („for“), Schwere, optionale Einschränkung auf Cluster, Node, Tag, VMID oder Storage, dazu Kanäle und eine optionale Wiederholung. Ist der Zustand wieder normal, wird der Alarm automatisch aufgelöst. Auf Wunsch geht dann eine Entwarnung raus. „Bestätigen“ stoppt die Wiederholungen.

**Kanäle:**

| Typ | Konfiguration |
|---|---|
| ntfy | Server, Topic, optional Access-Token. Versand per JSON, damit Umlaute erhalten bleiben. Die Priorität richtet sich nach der Schwere. |
| Gotify | Server, App-Token |
| E-Mail | SMTP-Server, Port, `starttls`/`ssl`/`none`, optional Login, Absender, Empfänger |
| Webhook | URL, optional HMAC-Secret. Die Signatur steht im Header `X-Signature-256: sha256=…`. JSON-Payload mit `event`, `title`, `rule`, `severity`, `target`, `message`, `value` |
| Home Assistant | HA-URL und Webhook-ID. In HA eine Automation mit Webhook-Trigger anlegen. |

Zugangsdaten werden verschlüsselt gespeichert und in der API nur maskiert angezeigt. Mit **Test senden** prüfst du einen Kanal. Trage `DASHBOARD_URL` ein, damit Benachrichtigungen einen Link enthalten.

## API-Keys und Prometheus

Unter **Admin → API-Keys** legst du Keys für Tools an. Der Key wird nur einmal angezeigt, gespeichert wird nur sein Hash.

| Scope | entspricht Rolle | Typisch für |
|---|---|---|
| `read` | Viewer | Home Assistant, Prometheus, Grafana, Skripte |
| `control` | Operator | MCP-Server / KI-Assistent mit Steuerung |
| `admin` | Admin | Node-Aktionen, Alarm-Regeln (keine Benutzer- und Key-Verwaltung) |

```bash
curl -H "Authorization: Bearer pvd_…" http://dashboard.lan:8080/api/v1/guests?sort=cpu_host\&order=desc\&limit=5
```

Keys brauchen kein CSRF-Token. Im Audit-Log erscheinen sie als `apikey:<name>`.

**Prometheus** unter `/metrics` mit einem `read`-Key:

```yaml
scrape_configs:
  - job_name: proxmox-dashboard
    metrics_path: /metrics
    authorization: { credentials: pvd_… }
    static_configs: [{ targets: ['dashboard.lan:8080'] }]
```

Exportiert werden `pve_cluster_up`, `pve_cluster_quorate`, `pve_node_*` (CPU, RAM, Swap, IO-Delay, Load, Uptime, Info, Wartung, offene Updates), `pve_guest_*` (Status, Info inkl. Tags und Pool, CPU, Host-Anteil, RAM, Disk- und Netzraten, Uptime), `pve_storage_*` und `pve_alerts_firing{severity}`.

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

| GET | `/api/v1/node-status` | Wartung, offene Updates, Neustart nötig, Temperatur, SSH-Status je Node |
| GET | `/api/v1/clusters/{c}/nodes/{n}/updates[/changelog?package=]` | Update-Liste, Changelog |
| POST | `/api/v1/clusters/{c}/nodes/{n}/updates/refresh` | `apt-get update` (Job) |
| POST | `/api/v1/clusters/{c}/nodes/{n}/upgrade` | `{confirm: <node>}`, dist-upgrade per SSH (Job) |
| GET/POST | `/api/v1/clusters/{c}/nodes/{n}/maintenance[/plan]` | `{enable, confirm, shutdown_unmigratable, return_guests}` (Job) |
| GET/POST | `/api/v1/clusters/{c}/nodes/{n}/reboot[/precheck]` | `{confirm, force}` (Job) |
| GET | `/api/v1/jobs`, `/api/v1/jobs/{id}`, `/api/v1/jobs/{id}/log`, `/api/v1/jobs/{id}/stream` (SSE) | Jobs und Live-Log |
| GET/POST | `/api/v1/admin/ssh`, `…/scan`, `…/trust`, `…/test` | SSH-Einrichtung |
| GET | `/api/v1/alerts?state=firing\|resolved\|all`, POST `/api/v1/alerts/{id}/ack` | Alarme |
| GET/POST/PUT/DELETE | `/api/v1/alerts/rules[/{id}]`, `/api/v1/alerts/channels[/{id}]`, POST `…/channels/{id}/test` | Regeln und Kanäle |
| GET/POST/DELETE | `/api/v1/admin/api-keys[/{id}]` | API-Keys (nur angemeldete Admins) |
| GET | `/metrics` | Prometheus |

Beispiele:

```bash
# Wer hat in den letzten 24 h auf pve1 die meiste CPU verbraucht?
curl -s -H "Authorization: Bearer $KEY" 'http://localhost:8080/api/v1/analysis/top?metric=cpu&node=pve1&range=24h&limit=5' | jq '.[] | {name, avg, p95, max}'

# Was lief am 24.10. um 10:15 Uhr auf pve1?
curl -s -H "Authorization: Bearer $KEY" "http://localhost:8080/api/v1/analysis/at?cluster=homelab&node=pve1&at=$(date -d '2026-10-24 10:15' +%s)&window=300" | jq '.guests[:5]'
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
  pve/mock_actions.py   Mock: Tasks, Statuswechsel, Snapshots, Migration, apt, Reboot
  pve/mock_ssh.py       Mock: SSH-Hilfsskript (Upgrade-Ausgabe, DMI, Sensoren)
  nodes/service.py      Updates, Upgrade, Wartungsmodus, Neustart, Sensoren, Host-Keys
  jobs.py               Jobs mit Live-Log (SSE) und Log-Dateien
  ssh.py                asyncssh-Runner mit Host-Key-Prüfung
  alerts/               Metrik-Katalog, Regel-Engine, Benachrichtigungen
  auth/apikeys.py       API-Keys mit Scopes
  api/prometheus.py     /metrics
  core/web.py           Security-Header, CSP, Auslieferung der SPA
frontend/src/
  lib/live.svelte.ts    SSE-Client + Live-Historie im Browser
  lib/auth.svelte.ts    Sitzung, Rollen, Fähigkeiten
  routes/               Übersicht, Nodes, Gäste (+ Bulk), Gast-Detail (+ Steuerung), Lastanalyse,
                        Inventar, Aufgaben, Audit-Log, Benutzer, Konto, Login
deploy/pve-setup-roles.sh   Proxmox-Rollen und -Tokens
deploy/node-setup.sh        SSH-Zugang auf den Nodes (eingeschränkt)
```
