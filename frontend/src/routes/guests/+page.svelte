<script lang="ts">
	import { page } from '$app/state';
	import { live } from '#lib/live.svelte.js';
	import { bytes, pct, rate, ratio, uptime } from '#lib/format.js';
	import Meter from '#lib/components/Meter.svelte';
	import StatusBadge from '#lib/components/StatusBadge.svelte';
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

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[1200px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr>
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
				<tr><td colspan="11" class="px-4 py-8 text-center text-zinc-500">Keine Gäste für diese Filter.</td></tr>
			{/each}
		</tbody>
	</table>
</div>
