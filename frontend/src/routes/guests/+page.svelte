<script lang="ts">
	import { page } from '$app/state';
	import { live } from '#lib/live.svelte.js';
	import { bytes, pct, rate, ratio, uptime } from '#lib/format.js';
	import Meter from '#lib/components/Meter.svelte';
	import StatusBadge from '#lib/components/StatusBadge.svelte';
	import ConfirmDialog from '#lib/components/ConfirmDialog.svelte';
	import { send } from '#lib/api.js';
	import { auth } from '#lib/auth.svelte.js';
	import { toasts } from '#lib/toast.svelte.js';
	import type { GuestView } from '#lib/types.js';

	type SortKey =
		| 'vmid' | 'name' | 'node' | 'status' | 'cpu' | 'cpu_host' | 'mem'
		| 'disk_io' | 'net_io' | 'uptime';

	const columns: { key: SortKey; label: string; numeric?: boolean; cls?: string }[] = [
		{ key: 'vmid', label: 'ID / Name' },
		{ key: 'node', label: 'Node' },
		{ key: 'status', label: 'Status' },
		{ key: 'cpu', label: 'CPU', numeric: true, cls: 'w-36' },
		{ key: 'cpu_host', label: 'Host-Anteil', numeric: true },
		{ key: 'mem', label: 'RAM', numeric: true, cls: 'w-44' },
		{ key: 'disk_io', label: 'Disk R / W', numeric: true },
		{ key: 'net_io', label: 'Netz ↓ / ↑', numeric: true },
		{ key: 'uptime', label: 'Uptime', numeric: true }
	];

	// Filters (initial sort may come from the URL, e.g. /guests?sort=cpu_host).
	let search = $state('');
	let node = $state('');
	let type = $state<'' | 'qemu' | 'lxc'>('');
	let status = $state('');
	let tag = $state('');
	let sortKey = $state<SortKey>((page.url.searchParams.get('sort') as SortKey) || 'vmid');
	let desc = $state(page.url.searchParams.has('sort'));

	const all = $derived(live.visible.flatMap((c) => c.guests).filter((g) => !g.template));
	const nodeNames = $derived([...new Set(all.map((g) => g.node))].sort());
	const tags = $derived([...new Set(all.flatMap((g) => g.tags))].sort());
	const multi = $derived(live.visible.length > 1);

	function value(g: GuestView, key: SortKey): number | string {
		switch (key) {
			case 'disk_io':
				return (g.diskread_bps ?? 0) + (g.diskwrite_bps ?? 0);
			case 'net_io':
				return (g.netin_bps ?? 0) + (g.netout_bps ?? 0);
			case 'name':
				return g.name.toLowerCase();
			default:
				return g[key];
		}
	}

	const rows = $derived.by(() => {
		const q = search.trim().toLowerCase();
		const filtered = all.filter(
			(g) =>
				(!q || g.name.toLowerCase().includes(q) || String(g.vmid).includes(q)) &&
				(!node || g.node === node) &&
				(!type || g.type === type) &&
				(!status || g.status === status) &&
				(!tag || g.tags.includes(tag))
		);
		const dir = desc ? -1 : 1;
		return filtered.sort((a, b) => {
			const va = value(a, sortKey);
			const vb = value(b, sortKey);
			return (va < vb ? -1 : va > vb ? 1 : a.vmid - b.vmid) * dir;
		});
	});

	function sortBy(key: SortKey, numeric = false) {
		if (sortKey === key) desc = !desc;
		else {
			sortKey = key;
			desc = numeric; // numbers: biggest first
		}
	}

	function reset() {
		search = node = type = status = tag = '';
	}

	// --- bulk actions (operators) ------------------------------------------------------------
	const key = (g: GuestView) => `${g.cluster}/${g.vmid}`;
	let selected = $state(new Set<string>());
	const canBulk = $derived(live.visible.some((c) => auth.canControl(c.name)));
	const chosen = $derived(rows.filter((g) => selected.has(key(g))));
	const allChosen = $derived(rows.length > 0 && rows.every((g) => selected.has(key(g))));

	function toggle(g: GuestView) {
		const next = new Set(selected);
		if (next.has(key(g))) next.delete(key(g));
		else next.add(key(g));
		selected = next;
	}
	function toggleAll() {
		selected = allChosen ? new Set() : new Set(rows.map(key));
	}

	type BulkAction = 'start' | 'shutdown' | 'reboot' | 'stop';
	const bulkLabels: Record<BulkAction, string> = {
		start: 'Starten', shutdown: 'Herunterfahren', reboot: 'Neu starten', stop: 'Hart stoppen'
	};
	let bulk = $state<BulkAction | null>(null);
	// Guests the action applies to (others are skipped by the backend anyway).
	const affected = $derived(
		bulk ? chosen.filter((g) => (bulk === 'start' ? g.status === 'stopped' : g.status === 'running')) : []
	);

	async function runBulk() {
		if (!bulk) return;
		const byCluster = new Map<string, number[]>();
		for (const g of chosen) byCluster.set(g.cluster, [...(byCluster.get(g.cluster) ?? []), g.vmid]);
		let ok = 0;
		const failed: string[] = [];
		for (const [cluster, vmids] of byCluster) {
			const count = affected.filter((g) => g.cluster === cluster).length;
			const res = await send<{ name: string; ok: boolean; skipped?: boolean; error?: string }[]>('POST', '/api/v1/actions/bulk', {
				cluster, action: bulk, vmids, confirm: bulk === 'stop' ? `stop ${count}` : undefined
			});
			for (const r of res) {
				if (r.ok && !r.skipped) ok++;
				else if (!r.ok) failed.push(`${r.name}: ${r.error}`);
			}
		}
		toasts.push(failed.length ? 'error' : 'ok', `${bulkLabels[bulk]}: ${ok} gestartet${failed.length ? `, Fehler: ${failed.join('; ')}` : ''}`);
		selected = new Set();
	}
</script>

<svelte:head><title>Gäste · Proxmox Dashboard</title></svelte:head>

<div class="mb-4 flex flex-wrap items-center gap-2">
	<h1 class="mr-auto text-xl font-semibold">Gäste <span class="text-base font-normal text-zinc-500">{rows.length}/{all.length}</span></h1>
	<input class="input w-full sm:w-48" type="search" placeholder="Name oder ID …" bind:value={search} aria-label="Suche" />
	<select class="input" bind:value={node} aria-label="Node">
		<option value="">Alle Nodes</option>
		{#each nodeNames as n (n)}<option value={n}>{n}</option>{/each}
	</select>
	<select class="input" bind:value={type} aria-label="Typ">
		<option value="">VM + LXC</option>
		<option value="qemu">nur VMs</option>
		<option value="lxc">nur LXC</option>
	</select>
	<select class="input" bind:value={status} aria-label="Status">
		<option value="">Jeder Status</option>
		<option value="running">läuft</option>
		<option value="stopped">gestoppt</option>
		<option value="paused">pausiert</option>
	</select>
	<select class="input" bind:value={tag} aria-label="Tag">
		<option value="">Alle Tags</option>
		{#each tags as t (t)}<option value={t}>{t}</option>{/each}
	</select>
	{#if search || node || type || status || tag}
		<button class="text-sm text-accent hover:underline" onclick={reset}>Filter zurücksetzen</button>
	{/if}
</div>

{#if canBulk && chosen.length}
	<div class="card mb-3 flex flex-wrap items-center gap-2 border-accent/50 py-2 text-sm">
		<span class="mr-2 font-medium">{chosen.length} ausgewählt</span>
		{#each Object.entries(bulkLabels) as [a, label] (a)}
			<button class="rounded-lg border border-zinc-300 px-3 py-1 hover:border-accent hover:text-accent dark:border-zinc-700 {a === 'stop' ? 'text-red-500' : ''}" onclick={() => (bulk = a as BulkAction)}>{label}</button>
		{/each}
		<button class="ml-auto text-xs text-zinc-500 hover:underline" onclick={() => (selected = new Set())}>Auswahl aufheben</button>
	</div>
{/if}

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[1320px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr>
				{#if canBulk}
					<th class="w-8 pl-3"><input type="checkbox" checked={allChosen} onchange={toggleAll} aria-label="Alle auswählen" /></th>
				{/if}
				{#each columns as col (col.key)}
					<th class="px-3 py-3 whitespace-nowrap {col.cls ?? ''}" aria-sort={sortKey === col.key ? (desc ? 'descending' : 'ascending') : 'none'}>
						<button class="flex items-center gap-1 uppercase hover:text-accent" onclick={() => sortBy(col.key, col.numeric)}>
							{col.label}
							<span class="text-accent" class:invisible={sortKey !== col.key}>{desc ? '↓' : '↑'}</span>
						</button>
					</th>
				{/each}
				<th class="px-3 py-3">Tags</th>
				<th class="px-3 py-3"><span class="sr-only">Konsole</span></th>
			</tr>
		</thead>
		<tbody>
			{#each rows as g (g.cluster + g.vmid)}
				{@const running = g.status === 'running'}
				<tr class="border-b border-zinc-100 last:border-0 hover:bg-zinc-50 dark:border-zinc-800/60 dark:hover:bg-zinc-800/40">
					{#if canBulk}
						<td class="pl-3">
							{#if auth.canControl(g.cluster)}<input type="checkbox" checked={selected.has(key(g))} onchange={() => toggle(g)} aria-label="{g.name} auswählen" />{/if}
						</td>
					{/if}
					<td class="px-3 py-2.5">
						<div class="flex items-center gap-2">
							<span class="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-semibold text-zinc-500 dark:bg-zinc-800">{g.type === 'qemu' ? 'VM' : 'LXC'}</span>
							<span class="tabular text-zinc-500">{g.vmid}</span>
							<a href="/guests/{g.cluster}/{g.vmid}" class="font-medium hover:text-accent">{g.name}</a>
							{#if g.lock}<span class="text-xs text-amber-500" title="Lock: {g.lock}">🔒</span>{/if}
						</div>
					</td>
					<td class="px-3 py-2.5">
						<a href="/nodes/{g.cluster}/{g.node}" class="hover:text-accent">{g.node}</a>
						{#if multi}<div class="text-xs text-zinc-500">{g.cluster}</div>{/if}
					</td>
					<td class="px-3 py-2.5"><StatusBadge status={g.status} /></td>
					<td class="px-3 py-2.5">
						{#if running}<Meter value={g.cpu} text="{pct(g.cpu)} · {g.maxcpu} vCPU" />{:else}<span class="text-zinc-500">{g.maxcpu} vCPU</span>{/if}
					</td>
					<td class="tabular px-3 py-2.5 text-right whitespace-nowrap">{running ? pct(g.cpu_host, 1) : '–'}</td>
					<td class="px-3 py-2.5">
						{#if running}
							<Meter value={ratio(g.mem, g.maxmem)} text="{bytes(g.mem)} / {bytes(g.maxmem)}" warn={0.85} crit={0.95} />
						{:else}<span class="text-zinc-500">{bytes(g.maxmem)}</span>{/if}
					</td>
					<td class="tabular px-3 py-2.5 text-right text-xs whitespace-nowrap">
						{#if running}{rate(g.diskread_bps)}<br /><span class="text-zinc-500">{rate(g.diskwrite_bps)}</span>{:else}–{/if}
					</td>
					<td class="tabular px-3 py-2.5 text-right text-xs whitespace-nowrap">
						{#if running}{rate(g.netin_bps)}<br /><span class="text-zinc-500">{rate(g.netout_bps)}</span>{:else}–{/if}
					</td>
					<td class="tabular px-3 py-2.5 text-right whitespace-nowrap">{running ? uptime(g.uptime) : '–'}</td>
					<td class="px-3 py-2.5">
						<div class="flex flex-wrap gap-1">
							{#each g.tags as t (t)}
								<button class="rounded bg-accent-soft px-1.5 py-0.5 text-xs text-accent" onclick={() => (tag = t)}>{t}</button>
							{/each}
						</div>
					</td>
					<td class="px-3 py-2.5 text-right whitespace-nowrap">
						{#if g.console_url && running}
							<a href={g.console_url} target="_blank" rel="noopener noreferrer" class="text-xs text-accent hover:underline" title="Konsole in der Proxmox-Oberfläche öffnen">Konsole ↗</a>
						{/if}
					</td>
				</tr>
			{:else}
				<tr><td colspan="12" class="px-4 py-8 text-center text-zinc-500">Keine Gäste für diese Filter.</td></tr>
			{/each}
		</tbody>
	</table>
</div>

{#if bulk}
	{@const b = bulk}
	<ConfirmDialog
		title="{bulkLabels[b]}: {affected.length} von {chosen.length} Gästen"
		message={affected.length ? `Betrifft: ${affected.map((g) => g.name).join(', ')}. Gäste, die bereits im Zielzustand sind, werden übersprungen.` : 'Keiner der ausgewählten Gäste ist betroffen.'}
		danger={b === 'stop'}
		requireText={b === 'stop' ? `stop ${affected.length}` : ''}
		confirmLabel={bulkLabels[b]}
		onconfirm={runBulk}
		onclose={() => (bulk = null)} />
{/if}
