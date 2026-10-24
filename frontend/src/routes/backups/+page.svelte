<script lang="ts">
	import { getJson, send } from '#lib/api.js';
	import { auth } from '#lib/auth.svelte.js';
	import { live } from '#lib/live.svelte.js';
	import { bytes, dateTime, timeAgo } from '#lib/format.js';
	import StatusBadge from '#lib/components/StatusBadge.svelte';
	import type { BackupJob, GuestBackups } from '#lib/types.js';

	interface Overview {
		updated_at: string | null;
		max_age_hours: number;
		errors: Record<string, string>;
		counts: Record<string, number>;
		guests: GuestBackups[];
	}

	let data = $state<Overview | null>(null);
	let jobs = $state<BackupJob[]>([]);
	let filter = $state<'' | GuestBackups['state'] | 'snapshots'>('');
	let refreshing = $state(false);

	const STATES: Record<GuestBackups['state'], { label: string; badge: string }> = {
		ok: { label: 'aktuell', badge: 'ok' },
		old: { label: 'zu alt', badge: 'warning' },
		missing: { label: 'kein Backup', badge: 'critical' },
		not_covered: { label: 'kein Job', badge: 'unknown' },
		failed_verify: { label: 'Verify fehlgeschlagen', badge: 'critical' }
	};

	async function load() {
		const cluster = live.selected ?? undefined;
		[data, jobs] = await Promise.all([
			getJson<Overview>('/api/v1/backups', { cluster }),
			getJson<BackupJob[]>('/api/v1/backups/jobs', { cluster })
		]);
	}

	$effect(() => {
		void live.selected;
		load();
		const t = setInterval(load, 60_000);
		return () => clearInterval(t);
	});

	async function refresh() {
		refreshing = true;
		try {
			await send('POST', '/api/v1/backups/refresh');
			await load();
		} finally {
			refreshing = false;
		}
	}

	const rows = $derived(
		(data?.guests ?? []).filter((g) =>
			!filter ? true : filter === 'snapshots' ? (g.oldest_snapshot_days ?? 0) > 30 : g.state === filter
		)
	);
	const age = (h: number | null) => (h == null ? '–' : h < 48 ? `${Math.round(h)} h` : `${Math.round(h / 24)} T`);
	const multi = $derived(live.visible.length > 1);
</script>

<svelte:head><title>Backups · Proxmox Dashboard</title></svelte:head>

<div class="mb-4 flex flex-wrap items-center gap-2">
	<h1 class="mr-auto text-xl font-semibold">Backups & Snapshots</h1>
	<span class="text-xs text-zinc-500">Stand: {timeAgo(data?.updated_at ?? null)}</span>
	<button class="input hover:border-accent" onclick={refresh} disabled={refreshing}>{refreshing ? 'Lese …' : 'Aktualisieren'}</button>
</div>

{#each Object.entries(data?.errors ?? {}) as [c, e] (c)}
	<div class="mb-3 rounded-lg bg-amber-500/10 px-3 py-2 text-sm text-amber-600">{c}: {e}</div>
{/each}

<div class="mb-4 flex flex-wrap gap-2">
	<button class="rounded-full px-3 py-1 text-sm {filter === '' ? 'bg-accent text-white' : 'bg-zinc-200 dark:bg-zinc-800'}" onclick={() => (filter = '')}>Alle {data?.guests.length ?? 0}</button>
	{#each Object.entries(STATES) as [k, s] (k)}
		{#if data?.counts[k]}
			<button class="rounded-full px-3 py-1 text-sm {filter === k ? 'bg-accent text-white' : 'bg-zinc-200 dark:bg-zinc-800'}" onclick={() => (filter = k as GuestBackups['state'])}>{s.label} {data.counts[k]}</button>
		{/if}
	{/each}
	<button class="rounded-full px-3 py-1 text-sm {filter === 'snapshots' ? 'bg-accent text-white' : 'bg-zinc-200 dark:bg-zinc-800'}" onclick={() => (filter = 'snapshots')}>Snapshots &gt; 30 Tage</button>
</div>

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[1000px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr><th class="px-4 py-3">Gast</th><th class="px-4 py-3">Status</th><th class="px-4 py-3">Letztes Backup</th><th class="px-4 py-3 text-right">Alter</th><th class="px-4 py-3">Ziel</th><th class="px-4 py-3 text-right">Anzahl</th><th class="px-4 py-3">Jobs</th><th class="px-4 py-3">Snapshots</th></tr>
		</thead>
		<tbody>
			{#each rows as g (g.cluster + g.vmid)}
				<tr class="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60">
					<td class="px-4 py-2"><a href="/guests/{g.cluster}/{g.vmid}" class="hover:text-accent"><span class="text-zinc-500">{g.vmid}</span> {g.name}</a>
						<div class="text-xs text-zinc-500">{g.type === 'qemu' ? 'VM' : 'LXC'} · {g.node}{multi ? ` · ${g.cluster}` : ''}</div></td>
					<td class="px-4 py-2"><StatusBadge status={STATES[g.state].badge} label={STATES[g.state].label} /></td>
					<td class="px-4 py-2 text-xs">{g.last_backup ? dateTime(Date.parse(g.last_backup.ctime) / 1000) : '–'}</td>
					<td class="tabular px-4 py-2 text-right {g.age_hours != null && g.age_hours > (data?.max_age_hours ?? 48) ? 'text-amber-500' : ''}">{age(g.age_hours)}</td>
					<td class="px-4 py-2 text-xs">{g.last_backup?.storage ?? '–'}{#if g.last_backup?.size}<span class="text-zinc-500"> · {bytes(g.last_backup.size)}</span>{/if}
						{#if g.last_backup?.verified}<div class={g.last_backup.verified === 'ok' ? 'text-emerald-500' : 'text-red-500'}>verify: {g.last_backup.verified}</div>{/if}</td>
					<td class="tabular px-4 py-2 text-right">{g.backup_count}</td>
					<td class="px-4 py-2 font-mono text-xs">{g.covered_by.join(', ') || '–'}</td>
					<td class="px-4 py-2 text-xs">{g.snapshots || '–'}{#if g.oldest_snapshot_days != null}<span class={g.oldest_snapshot_days > 30 ? 'text-amber-500' : 'text-zinc-500'}> · ältester {Math.round(g.oldest_snapshot_days)} T</span>{/if}</td>
				</tr>
			{:else}
				<tr><td colspan="8" class="px-4 py-6 text-center text-zinc-500">{data ? 'Keine Einträge.' : 'Lade …'}</td></tr>
			{/each}
		</tbody>
	</table>
</div>

<div class="card mt-4 overflow-x-auto p-0">
	<div class="label px-4 pt-4">Backup-Jobs</div>
	<table class="mt-2 w-full min-w-[760px] text-sm">
		<thead class="text-left text-xs text-zinc-500"><tr><th class="px-4 py-2">Job</th><th class="px-4 py-2">Zeitplan</th><th class="px-4 py-2">Ziel</th><th class="px-4 py-2">Auswahl</th><th class="px-4 py-2 text-right">Gäste</th><th class="px-4 py-2">Modus</th></tr></thead>
		<tbody>
			{#each jobs as j (j.cluster + j.id)}
				<tr class="border-t border-zinc-100 dark:border-zinc-800/60" class:opacity-50={!j.enabled}>
					<td class="px-4 py-2 font-mono text-xs">{j.id}{#if !j.enabled} <span class="text-zinc-500">(deaktiviert)</span>{/if}{#if j.comment}<div class="font-sans text-zinc-500">{j.comment}</div>{/if}</td>
					<td class="px-4 py-2">{j.schedule ?? '–'}</td>
					<td class="px-4 py-2">{j.storage ?? '–'}</td>
					<td class="px-4 py-2">{j.selection}</td>
					<td class="tabular px-4 py-2 text-right">{j.vmids.length}</td>
					<td class="px-4 py-2 text-xs">{j.mode ?? ''}</td>
				</tr>
			{:else}<tr><td colspan="6" class="px-4 py-4 text-zinc-500">Keine Backup-Jobs gefunden.</td></tr>{/each}
		</tbody>
	</table>
</div>
<p class="mt-2 text-xs text-zinc-500">„Aktuell“ = jüngstes Backup jünger als {data?.max_age_hours ?? 48} h (BACKUP_MAX_AGE_HOURS). {#if auth.can('admin')}Alarm-Regeln dazu unter <a href="/alerts" class="text-accent hover:underline">Alarme → Regeln</a>.{/if}</p>
