<script lang="ts">
	// Control panel for one guest: power, snapshots, backup, migration, recent tasks.
	// Visible to everyone; buttons only for operators on clusters with actions enabled.
	import { getJson, send } from '#lib/api.js';
	import { auth } from '#lib/auth.svelte.js';
	import { dateTime } from '#lib/format.js';
	import { taskViewer, toasts } from '#lib/toast.svelte.js';
	import type { GuestView } from '#lib/types.js';
	import ConfirmDialog from './ConfirmDialog.svelte';

	let { guest }: { guest: GuestView } = $props();

	interface Snapshot {
		name: string;
		description?: string;
		snaptime?: number;
		vmstate?: number;
		parent?: string;
	}
	interface Task {
		upid: string;
		type: string;
		starttime: number;
		status: string | null;
		user: string | null;
	}
	interface Precheck {
		allowed_nodes: string[];
		not_allowed: Record<string, { unavailable_resources?: string[] }>;
		local_disks: string[];
		local_resources: string[];
		online: boolean;
		note: string | null;
	}
	interface Result {
		upid: string | null;
		dry_run: boolean;
		message: string | null;
	}

	type Dialog =
		| { kind: 'power'; action: string; label: string; danger: boolean; message: string }
		| { kind: 'snapshot' }
		| { kind: 'rollback' | 'delete'; snap: string }
		| { kind: 'backup' }
		| { kind: 'migrate' };

	const base = $derived(`/api/v1/clusters/${encodeURIComponent(guest.cluster)}/guests/${guest.vmid}`);
	const can = $derived(auth.canControl(guest.cluster));
	const cap = $derived(auth.capabilities[guest.cluster]);
	const isVm = $derived(guest.type === 'qemu');
	const kind = $derived(isVm ? 'VM' : 'Container');

	let snapshots = $state<Snapshot[]>([]);
	let tasks = $state<Task[]>([]);
	let dialog = $state<Dialog | null>(null);

	// dialog inputs
	let snapName = $state('');
	let snapDesc = $state('');
	let snapRam = $state(false);
	let storages = $state<string[]>([]);
	let storage = $state('');
	let mode = $state<'snapshot' | 'suspend' | 'stop'>('snapshot');
	let precheck = $state<Precheck | null>(null);
	let target = $state('');

	async function refresh() {
		try {
			[snapshots, tasks] = await Promise.all([
				getJson<Snapshot[]>(`${base}/snapshots`),
				getJson<Task[]>('/api/v1/tasks', { cluster: guest.cluster, vmid: guest.vmid, limit: 8 })
			]);
		} catch {
			/* not critical */
		}
	}

	$effect(() => {
		void base;
		refresh();
		const timer = setInterval(refresh, 5000);
		return () => clearInterval(timer);
	});

	function started(label: string, r: Result) {
		if (r.dry_run) {
			toasts.push('info', `${label}: ${r.message ?? 'Dry-Run'}`);
		} else if (r.upid) {
			const upid = r.upid;
			toasts.push('ok', `${label} gestartet`, { label: 'Log', run: () => taskViewer.open(guest.cluster, upid) });
		} else {
			toasts.push('ok', `${label} ausgeführt`);
		}
		setTimeout(refresh, 800);
	}

	// --- power ---------------------------------------------------------------------------------
	const powerButtons = $derived.by(() => {
		const s = guest.status;
		const b: { action: string; label: string; danger: boolean; message: string }[] = [];
		if (s === 'stopped') b.push({ action: 'start', label: 'Starten', danger: false, message: `${kind} ${guest.name} starten?` });
		if (s === 'running') {
			b.push({ action: 'shutdown', label: 'Herunterfahren', danger: false, message: `${kind} ${guest.name} sauber herunterfahren (ACPI bzw. Guest Agent)?` });
			b.push({ action: 'reboot', label: 'Neu starten', danger: false, message: `${kind} ${guest.name} neu starten?` });
			b.push({ action: 'suspend', label: 'Pausieren', danger: false, message: `${kind} ${guest.name} pausieren? Die Ausführung wird angehalten, der RAM bleibt belegt.` });
			b.push({ action: 'stop', label: 'Stoppen (hart)', danger: true, message: `Harter Stopp entspricht dem Ziehen des Stromkabels – ungespeicherte Daten gehen verloren.` });
		}
		if (s === 'paused' || s === 'suspended') {
			b.push({ action: 'resume', label: 'Fortsetzen', danger: false, message: `${kind} ${guest.name} fortsetzen?` });
			b.push({ action: 'stop', label: 'Stoppen (hart)', danger: true, message: 'Harter Stopp – ungespeicherte Daten gehen verloren.' });
		}
		return b;
	});

	async function doPower(action: string, label: string, typed: string) {
		const r = await send<Result>('POST', `${base}/power`, { action, confirm: typed || undefined });
		started(label, r);
	}

	// --- snapshots / backup / migrate ----------------------------------------------------------
	async function openBackup() {
		storages = await getJson<string[]>(`${base}/backup-storages`);
		storage = storages.includes('pbs') ? 'pbs' : (storages[0] ?? '');
		dialog = { kind: 'backup' };
	}

	async function openMigrate() {
		precheck = null;
		target = '';
		dialog = { kind: 'migrate' };
		try {
			precheck = await getJson<Precheck>(`${base}/migrate`);
			target = precheck.allowed_nodes[0] ?? '';
		} catch (e) {
			toasts.push('error', (e as Error).message);
			dialog = null;
		}
	}

	function openSnapshot() {
		const d = new Date();
		snapName = `manuell-${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}-${String(d.getHours()).padStart(2, '0')}${String(d.getMinutes()).padStart(2, '0')}`;
		snapDesc = '';
		snapRam = false;
		dialog = { kind: 'snapshot' };
	}

	const needsName = $derived(dialog?.kind === 'migrate' && !isVm && guest.status === 'running');
	const btn = 'rounded-lg border border-zinc-300 px-3 py-1.5 text-sm hover:border-accent hover:text-accent disabled:opacity-40 dark:border-zinc-700';
</script>

<div class="card">
	<div class="flex flex-wrap items-center justify-between gap-2">
		<span class="label">Steuerung</span>
		{#if !auth.can('operator')}
			<span class="text-xs text-zinc-500">Nur lesend (Rolle {auth.me?.role})</span>
		{:else if !cap?.enabled}
			<span class="text-xs text-amber-500">{cap?.reason ?? 'Steuerung nicht verfügbar'}</span>
		{:else if cap.dry_run}
			<span class="rounded bg-violet-500/15 px-2 py-0.5 text-xs text-violet-500">Dry-Run: Aktionen werden nur protokolliert</span>
		{/if}
	</div>

	{#if can}
		<div class="mt-3 flex flex-wrap gap-2">
			{#each powerButtons as b (b.action)}
				<button class="{btn} {b.danger ? 'border-red-500/40 text-red-500 hover:border-red-500 hover:text-red-500' : ''}" onclick={() => (dialog = { kind: 'power', ...b })}>{b.label}</button>
			{/each}
			<span class="mx-1 hidden w-px bg-zinc-200 sm:block dark:bg-zinc-800"></span>
			<button class={btn} onclick={openSnapshot}>Snapshot</button>
			<button class={btn} onclick={openBackup}>Backup jetzt</button>
			<button class={btn} onclick={openMigrate}>Migrieren</button>
		</div>
	{/if}

	<div class="mt-5 grid gap-6 lg:grid-cols-2">
		<div>
			<div class="mb-1 text-xs text-zinc-500">Snapshots ({snapshots.length})</div>
			<ul class="flex flex-col divide-y divide-zinc-100 text-sm dark:divide-zinc-800">
				{#each snapshots as s (s.name)}
					<li class="flex items-center justify-between gap-2 py-1.5">
						<div class="min-w-0">
							<div class="truncate font-mono text-xs">{s.name}{#if s.vmstate}<span class="ml-1 text-violet-500">+RAM</span>{/if}</div>
							<div class="truncate text-xs text-zinc-500">{s.snaptime ? dateTime(s.snaptime) : ''}{s.description ? ` · ${s.description}` : ''}</div>
						</div>
						{#if can}
							<div class="flex shrink-0 gap-2 text-xs">
								<button class="hover:text-accent" onclick={() => (dialog = { kind: 'rollback', snap: s.name })}>Zurücksetzen</button>
								<button class="text-red-500 hover:underline" onclick={() => (dialog = { kind: 'delete', snap: s.name })}>Löschen</button>
							</div>
						{/if}
					</li>
				{:else}
					<li class="py-1.5 text-zinc-500">Keine Snapshots</li>
				{/each}
			</ul>
		</div>
		<div>
			<div class="mb-1 text-xs text-zinc-500">Letzte Aufgaben</div>
			<ul class="flex flex-col divide-y divide-zinc-100 text-sm dark:divide-zinc-800">
				{#each tasks as t (t.upid)}
					<li>
						<button class="flex w-full items-center justify-between gap-2 py-1.5 text-left hover:text-accent" onclick={() => taskViewer.open(guest.cluster, t.upid)}>
							<span class="font-mono text-xs">{t.type}</span>
							<span class="text-xs text-zinc-500">{dateTime(t.starttime)}</span>
							<span class="text-xs {t.status === null ? 'text-blue-500' : t.status === 'OK' ? 'text-emerald-500' : 'text-red-500'}">{t.status ?? 'läuft …'}</span>
						</button>
					</li>
				{:else}
					<li class="py-1.5 text-zinc-500">Keine</li>
				{/each}
			</ul>
		</div>
	</div>
</div>

{#if dialog?.kind === 'power'}
	{@const d = dialog}
	<ConfirmDialog title="{d.label}: {guest.vmid} {guest.name}" message={d.message} danger={d.danger}
		requireText={d.danger ? guest.name : ''} confirmLabel={d.label}
		onconfirm={(typed) => doPower(d.action, d.label, typed)} onclose={() => (dialog = null)} />
{:else if dialog?.kind === 'snapshot'}
	<ConfirmDialog title="Snapshot von {guest.name}" message="Erstellt einen Snapshot des aktuellen Zustands." confirmLabel="Erstellen"
		onconfirm={async () => started('Snapshot', await send<Result>('POST', `${base}/snapshots`, { name: snapName, description: snapDesc, vmstate: snapRam }))}
		onclose={() => (dialog = null)}>
		<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Name (Buchstabe zuerst, A–Z 0–9 _ -)</span><input class="input font-mono" bind:value={snapName} maxlength="40" /></label>
		<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Beschreibung</span><input class="input" bind:value={snapDesc} maxlength="500" placeholder="z. B. vor Update" /></label>
		{#if isVm && guest.status === 'running'}
			<label class="flex items-center gap-2"><input type="checkbox" bind:checked={snapRam} /> RAM-Inhalt mitsichern (langsamer, braucht Platz)</label>
		{/if}
	</ConfirmDialog>
{:else if dialog?.kind === 'rollback'}
	{@const snap = dialog.snap}
	<ConfirmDialog title="Auf Snapshot „{snap}“ zurücksetzen?" danger requireText={guest.name} confirmLabel="Zurücksetzen"
		message="Alle Änderungen seit dem Snapshot gehen verloren. {isVm ? 'Die VM' : 'Der Container'} wird dabei ggf. gestoppt."
		onconfirm={async (typed) => started('Rollback', await send<Result>('POST', `${base}/snapshots/${encodeURIComponent(snap)}/rollback`, { confirm: typed }))}
		onclose={() => (dialog = null)} />
{:else if dialog?.kind === 'delete'}
	{@const snap = dialog.snap}
	<ConfirmDialog title="Snapshot „{snap}“ löschen?" danger requireText={guest.name} confirmLabel="Löschen"
		message="Der Snapshot wird endgültig entfernt."
		onconfirm={async (typed) => started('Snapshot löschen', await send<Result>('DELETE', `${base}/snapshots/${encodeURIComponent(snap)}`, undefined, { confirm: typed }))}
		onclose={() => (dialog = null)} />
{:else if dialog?.kind === 'backup'}
	<ConfirmDialog title="Backup von {guest.name}" message="Startet sofort ein Backup (vzdump, zstd)." confirmLabel="Backup starten"
		onconfirm={async () => started('Backup', await send<Result>('POST', `${base}/backup`, { storage, mode }))}
		onclose={() => (dialog = null)}>
		<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Ziel-Storage</span>
			<select class="input" bind:value={storage}>{#each storages as s (s)}<option value={s}>{s}</option>{/each}</select>
		</label>
		<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Modus</span>
			<select class="input" bind:value={mode}>
				<option value="snapshot">Snapshot – ohne Unterbrechung (empfohlen)</option>
				<option value="suspend">Suspend – kurz pausieren</option>
				<option value="stop">Stop – herunterfahren, sichern, wieder starten</option>
			</select>
		</label>
	</ConfirmDialog>
{:else if dialog?.kind === 'migrate'}
	<ConfirmDialog title="{guest.name} migrieren" danger={needsName} requireText={needsName ? guest.name : ''}
		message="Aktuell auf {guest.node}. {precheck?.online ? 'Live-Migration ohne Unterbrechung.' : ''}"
		confirmLabel="Migrieren"
		onconfirm={async (typed) => {
			if (!target) throw new Error('Kein Ziel-Node verfügbar');
			started('Migration', await send<Result>('POST', `${base}/migrate`, { target, confirm: typed || undefined }));
		}}
		onclose={() => (dialog = null)}>
		{#if !precheck}
			<p class="text-zinc-500">Prüfe Voraussetzungen …</p>
		{:else}
			{#if precheck.allowed_nodes.length}
				<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Ziel-Node</span>
					<select class="input" bind:value={target}>{#each precheck.allowed_nodes as n (n)}<option value={n}>{n}</option>{/each}</select>
				</label>
			{:else}
				<p class="rounded-lg bg-red-500/10 px-3 py-2 text-red-500">Keine Migration möglich.</p>
			{/if}
			{#if precheck.local_resources.length}<p class="text-amber-500">Lokale Ressourcen: {precheck.local_resources.join(', ')} – verhindern die Migration.</p>{/if}
			{#if precheck.local_disks.length}<p class="text-xs text-zinc-500">Lokale Disks werden mitkopiert: {precheck.local_disks.join(', ')}</p>{/if}
			{#if precheck.note}<p class="text-xs text-amber-500">{precheck.note}</p>{/if}
		{/if}
	</ConfirmDialog>
{/if}
