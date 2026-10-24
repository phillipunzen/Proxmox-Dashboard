<script lang="ts">
	import { getJson } from '#lib/api.js';
	import { live } from '#lib/live.svelte.js';
	import { dateTime } from '#lib/format.js';
	import { taskViewer } from '#lib/toast.svelte.js';

	interface Task {
		cluster: string;
		upid: string;
		node: string;
		type: string;
		id: string | null;
		user: string | null;
		starttime: number;
		endtime: number | null;
		status: string | null;
	}

	let tasks = $state<Task[]>([]);
	let errorsOnly = $state(false);
	let error = $state<string | null>(null);

	// Readable names for common task types.
	const TYPES: Record<string, string> = {
		qmstart: 'VM starten', qmshutdown: 'VM herunterfahren', qmstop: 'VM stoppen', qmreboot: 'VM neu starten',
		qmsuspend: 'VM pausieren', qmresume: 'VM fortsetzen', qmsnapshot: 'Snapshot', qmrollback: 'Rollback',
		qmdelsnapshot: 'Snapshot löschen', qmigrate: 'VM migrieren', vzstart: 'CT starten', vzshutdown: 'CT herunterfahren',
		vzstop: 'CT stoppen', vzreboot: 'CT neu starten', vzsnapshot: 'Snapshot', vzrollback: 'Rollback',
		vzdelsnapshot: 'Snapshot löschen', vzmigrate: 'CT migrieren', vzdump: 'Backup', aptupdate: 'Paketlisten aktualisieren'
	};

	$effect(() => {
		const cluster = live.selected;
		const params = { cluster, errors_only: errorsOnly, limit: 200 };
		const load = () =>
			getJson<Task[]>('/api/v1/tasks', params)
				.then((t) => ((tasks = t), (error = null)))
				.catch((e) => (error = e.message));
		load();
		const timer = setInterval(load, 5000);
		return () => clearInterval(timer);
	});

	const name = (g: string | null, cluster: string) =>
		g ? live.clusters.find((c) => c.name === cluster)?.guests.find((x) => String(x.vmid) === g)?.name : undefined;
	const duration = (t: Task) => {
		const s = (t.endtime ?? Date.now() / 1000) - t.starttime;
		return s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
	};
</script>

<svelte:head><title>Aufgaben · Proxmox Dashboard</title></svelte:head>

<div class="mb-4 flex flex-wrap items-center gap-3">
	<h1 class="mr-auto text-xl font-semibold">Proxmox-Aufgaben</h1>
	<label class="flex items-center gap-2 text-sm"><input type="checkbox" bind:checked={errorsOnly} /> nur Fehler</label>
</div>
{#if error}<div class="mb-3 text-sm text-red-500">{error}</div>{/if}

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[900px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr><th class="px-4 py-3">Start</th><th class="px-4 py-3">Aufgabe</th><th class="px-4 py-3">Ziel</th><th class="px-4 py-3">Node</th><th class="px-4 py-3">Benutzer</th><th class="px-4 py-3">Dauer</th><th class="px-4 py-3">Status</th></tr>
		</thead>
		<tbody>
			{#each tasks as t (t.upid)}
				<tr class="cursor-pointer border-b border-zinc-100 last:border-0 hover:bg-zinc-50 dark:border-zinc-800/60 dark:hover:bg-zinc-800/40" onclick={() => taskViewer.open(t.cluster, t.upid)}>
					<td class="px-4 py-2 text-xs whitespace-nowrap">{dateTime(t.starttime)}</td>
					<td class="px-4 py-2">{TYPES[t.type] ?? t.type}</td>
					<td class="px-4 py-2">{t.id ?? ''} <span class="text-zinc-500">{name(t.id, t.cluster) ?? ''}</span></td>
					<td class="px-4 py-2">{t.node}</td>
					<td class="px-4 py-2 font-mono text-xs">{t.user}</td>
					<td class="tabular px-4 py-2">{duration(t)}</td>
					<td class="px-4 py-2">
						{#if t.status === null}<span class="text-blue-500">läuft …</span>
						{:else if t.status === 'OK'}<span class="text-emerald-500">OK</span>
						{:else}<span class="text-red-500">{t.status}</span>{/if}
					</td>
				</tr>
			{:else}
				<tr><td colspan="7" class="px-4 py-8 text-center text-zinc-500">Keine Aufgaben.</td></tr>
			{/each}
		</tbody>
	</table>
</div>
