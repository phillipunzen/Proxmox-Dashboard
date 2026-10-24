<script lang="ts">
	// Updates, upgrade, maintenance mode and reboot for one node (admins).
	import { getJson, send } from '#lib/api.js';
	import { auth } from '#lib/auth.svelte.js';
	import { dateTime, timeAgo } from '#lib/format.js';
	import { jobViewer, toasts } from '#lib/toast.svelte.js';
	import type { Job, NodeStatus, UpdateInfo } from '#lib/types.js';
	import ConfirmDialog from './ConfirmDialog.svelte';
	import Modal from './Modal.svelte';

	let { cluster, node }: { cluster: string; node: string } = $props();

	interface PlanItem {
		vmid: number;
		name: string;
		type: string;
		target: string | null;
		reason: string | null;
		restart: boolean;
	}
	interface Precheck {
		ok: boolean;
		blockers: string[];
		warnings: string[];
		quorum: string;
		running_guests: { vmid: number; name: string }[];
		maintenance: boolean;
	}

	const base = $derived(`/api/v1/clusters/${encodeURIComponent(cluster)}/nodes/${encodeURIComponent(node)}`);
	let status = $state<NodeStatus | null>(null);
	let updates = $state<UpdateInfo | null>(null);
	let jobs = $state<Job[]>([]);
	let dialog = $state<null | 'upgrade' | 'maint-on' | 'maint-off' | 'reboot'>(null);
	let plan = $state<PlanItem[]>([]);
	let precheck = $state<Precheck | null>(null);
	let shutdownUnmigratable = $state(false);
	let returnGuests = $state(true);
	let force = $state(false);
	let changelog = $state<{ pkg: string; text: string } | null>(null);

	const isAdmin = $derived(auth.can('admin'));
	const canManage = $derived(isAdmin && !!status?.can_manage);
	const busy = $derived(!!status?.running_job);

	async function load() {
		try {
			const [all, u, j] = await Promise.all([
				getJson<NodeStatus[]>('/api/v1/node-status', { cluster }),
				getJson<UpdateInfo>(`${base}/updates`),
				getJson<Job[]>('/api/v1/jobs', { cluster, node, limit: 8 })
			]);
			status = all.find((s) => s.node === node) ?? null;
			updates = u;
			jobs = j;
		} catch {
			/* shown as missing data */
		}
	}

	$effect(() => {
		void base;
		load();
		const timer = setInterval(load, 5000);
		return () => clearInterval(timer);
	});

	async function startJob(path: string, body: unknown, label: string) {
		const r = await send<{ job_id: number }>('POST', `${base}${path}`, body);
		toasts.push('ok', `${label} gestartet`, { label: 'Log', run: () => jobViewer.open(r.job_id) });
		jobViewer.open(r.job_id);
		load();
	}

	async function openMaintenance() {
		plan = await getJson<PlanItem[]>(`${base}/maintenance/plan`);
		shutdownUnmigratable = false;
		dialog = 'maint-on';
	}

	async function openReboot() {
		precheck = await getJson<Precheck>(`${base}/reboot/precheck`);
		force = false;
		dialog = 'reboot';
	}

	async function showChangelog(pkg: string, version: string) {
		try {
			const r = await getJson<{ changelog: string }>(`${base}/updates/changelog`, { package: pkg, version });
			changelog = { pkg, text: r.changelog };
		} catch (e) {
			toasts.push('error', (e as Error).message);
		}
	}

	const KIND: Record<string, string> = {
		upgrade: 'Upgrade', 'apt-refresh': 'Paketlisten', 'maintenance-on': 'Wartung an',
		'maintenance-off': 'Wartung aus', reboot: 'Neustart'
	};
	const btn = 'rounded-lg border border-zinc-300 px-3 py-1.5 text-sm hover:border-accent hover:text-accent disabled:opacity-40 dark:border-zinc-700';
</script>

<div class="card">
	<div class="flex flex-wrap items-center gap-2">
		<span class="label mr-auto">Verwaltung</span>
		{#if status?.maintenance}
			<span class="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-500">
				Wartungsmodus seit {status.maintenance_since ? timeAgo(status.maintenance_since) : '?'} ({status.maintenance_by})
			</span>
		{/if}
		{#if status?.reboot_required}<span class="rounded-full bg-violet-500/15 px-2 py-0.5 text-xs text-violet-500">Neustart erforderlich</span>{/if}
		{#if status?.temperature != null}<span class="text-xs {status.temperature >= 80 ? 'text-red-500' : 'text-zinc-500'}">🌡 {status.temperature.toFixed(0)} °C</span>{/if}
	</div>

	{#if isAdmin && !status?.can_manage}
		<p class="mt-2 text-xs text-amber-500">Node-Aktionen nicht verfügbar: {status?.manage_reason}</p>
	{/if}
	{#if isAdmin && status && !status.ssh_trusted}
		<p class="mt-2 text-xs text-zinc-500">SSH für {node} ist noch nicht eingerichtet → <a href="/admin/ssh" class="text-accent hover:underline">Admin → SSH</a> (für Upgrades, Mainboard, Temperaturen)</p>
	{/if}

	{#if canManage}
		<div class="mt-3 flex flex-wrap gap-2">
			<button class={btn} disabled={busy} onclick={() => startJob('/updates/refresh', undefined, 'Paketlisten aktualisieren')}>Paketlisten aktualisieren</button>
			<button class={btn} disabled={busy || !status?.ssh_trusted || !updates?.packages.length} onclick={() => (dialog = 'upgrade')}>Upgrade installieren ({updates?.packages.length ?? 0})</button>
			{#if status?.maintenance}
				<button class={btn} disabled={busy} onclick={() => { returnGuests = true; dialog = 'maint-off'; }}>Wartungsmodus beenden</button>
			{:else}
				<button class={btn} disabled={busy} onclick={openMaintenance}>Wartungsmodus</button>
			{/if}
			<button class="{btn} border-red-500/40 text-red-500" disabled={busy} onclick={openReboot}>Neustart</button>
			{#if busy}<button class="text-xs text-accent hover:underline" onclick={() => jobViewer.open(status!.running_job!)}>läuft: Job #{status?.running_job} ansehen</button>{/if}
		</div>
	{/if}

	<div class="mt-5 grid gap-6 lg:grid-cols-3">
		<div class="lg:col-span-2">
			<div class="mb-1 flex items-center justify-between text-xs text-zinc-500">
				<span>Verfügbare Updates</span>
				<span>{updates?.checked_at ? `geprüft ${timeAgo(updates.checked_at)}` : ''}</span>
			</div>
			{#if updates?.error}
				<p class="text-sm text-amber-500">{updates.error}</p>
			{:else if !updates?.packages.length}
				<p class="text-sm text-emerald-500">✓ Alles aktuell</p>
			{:else}
				<div class="max-h-72 overflow-auto">
					<table class="w-full text-sm">
						<tbody>
							{#each updates.packages as p (p.package)}
								<tr class="border-t border-zinc-100 first:border-0 dark:border-zinc-800/60">
									<td class="py-1 pr-2 font-mono text-xs">{p.package}</td>
									<td class="py-1 pr-2 font-mono text-xs text-zinc-500">{p.old_version} → <span class="text-zinc-800 dark:text-zinc-200">{p.new_version}</span></td>
									<td class="py-1 pr-2 text-xs text-zinc-500">{p.origin}</td>
									<td class="py-1 text-right"><button class="text-xs text-accent hover:underline" onclick={() => showChangelog(p.package, p.new_version)}>Changelog</button></td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		</div>
		<div>
			<div class="mb-1 text-xs text-zinc-500">Letzte Jobs</div>
			<ul class="flex flex-col divide-y divide-zinc-100 text-sm dark:divide-zinc-800">
				{#each jobs as j (j.id)}
					<li>
						<button class="flex w-full justify-between gap-2 py-1.5 text-left hover:text-accent" onclick={() => jobViewer.open(j.id)}>
							<span>{KIND[j.kind] ?? j.kind}</span>
							<span class="text-xs text-zinc-500">{dateTime(Date.parse(j.started_at) / 1000)}</span>
							<span class="text-xs {j.status === 'running' ? 'text-blue-500' : j.status === 'ok' ? 'text-emerald-500' : 'text-red-500'}">{j.status}</span>
						</button>
					</li>
				{:else}<li class="py-1.5 text-zinc-500">Keine</li>{/each}
			</ul>
		</div>
	</div>
</div>

{#if dialog === 'upgrade'}
	<ConfirmDialog title="Upgrade auf {node}" danger requireText={node} confirmLabel="Upgrade starten"
		message="Führt per SSH 'apt-get update && apt-get dist-upgrade' aus ({updates?.packages.length} Pakete). Die Ausgabe erscheint live. Während des Upgrades laufen die Gäste weiter; bei einem neuen Kernel ist danach ein Neustart nötig."
		onconfirm={(t) => startJob('/upgrade', { confirm: t }, 'Upgrade')} onclose={() => (dialog = null)} />
{:else if dialog === 'maint-on'}
	<ConfirmDialog title="Wartungsmodus für {node}" requireText={node} confirmLabel="Aktivieren"
		message="Laufende Gäste werden auf andere Nodes migriert (Ziel: Node mit dem meisten freien RAM)."
		onconfirm={(t) => startJob('/maintenance', { enable: true, confirm: t, shutdown_unmigratable: shutdownUnmigratable }, 'Wartungsmodus')}
		onclose={() => (dialog = null)}>
		<table class="w-full text-sm">
			<tbody>
				{#each plan as p (p.vmid)}
					<tr class="border-t border-zinc-100 first:border-0 dark:border-zinc-800">
						<td class="py-1">{p.vmid} {p.name}</td>
						<td class="py-1 text-xs">{#if p.target}→ {p.target}{p.restart ? ' (Neustart)' : ' (live)'}{:else}<span class="text-amber-500">{p.reason}</span>{/if}</td>
					</tr>
				{:else}<tr><td class="text-zinc-500">Keine laufenden Gäste.</td></tr>{/each}
			</tbody>
		</table>
		{#if plan.some((p) => !p.target)}
			<label class="flex items-center gap-2"><input type="checkbox" bind:checked={shutdownUnmigratable} /> nicht migrierbare Gäste sauber herunterfahren</label>
		{/if}
	</ConfirmDialog>
{:else if dialog === 'maint-off'}
	<ConfirmDialog title="Wartungsmodus für {node} beenden" confirmLabel="Beenden"
		message="{status?.evacuated.length ?? 0} Gast/Gäste wurden beim Aktivieren verschoben bzw. heruntergefahren."
		onconfirm={() => startJob('/maintenance', { enable: false, return_guests: returnGuests }, 'Wartung beenden')}
		onclose={() => (dialog = null)}>
		{#if status?.evacuated.length}
			<label class="flex items-center gap-2"><input type="checkbox" bind:checked={returnGuests} /> Gäste zurückholen bzw. wieder starten</label>
		{/if}
	</ConfirmDialog>
{:else if dialog === 'reboot' && precheck}
	{@const pc = precheck}
	<ConfirmDialog title="{node} neu starten" danger requireText={node} confirmLabel="Neustart"
		message={pc.ok ? 'Vorab-Prüfung:' : 'Neustart ist nicht möglich:'}
		onconfirm={async (t) => {
			if (!pc.ok) throw new Error(pc.blockers.join('; '));
			await startJob('/reboot', { confirm: t, force }, 'Neustart');
		}}
		onclose={() => (dialog = null)}>
		<ul class="space-y-1 text-sm">
			<li class={pc.blockers.length ? 'text-red-500' : 'text-emerald-500'}>{pc.quorum}</li>
			{#each pc.blockers as b (b)}<li class="text-red-500">✗ {b}</li>{/each}
			{#each pc.warnings as w (w)}<li class="text-amber-500">⚠ {w}</li>{/each}
		</ul>
		{#if pc.running_guests.length}
			<label class="flex items-center gap-2"><input type="checkbox" bind:checked={force} /> Trotzdem neu starten ({pc.running_guests.length} Gäste werden mit heruntergefahren)</label>
		{/if}
	</ConfirmDialog>
{/if}

{#if changelog}
	<Modal title="Changelog: {changelog.pkg}" onclose={() => (changelog = null)} width="760px">
		<pre class="font-mono text-xs whitespace-pre-wrap">{changelog.text}</pre>
	</Modal>
{/if}
