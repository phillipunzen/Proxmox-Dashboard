<script lang="ts">
	import { live } from '#lib/live.svelte.js';
	import { bytes, num, pct, ratio, uptime } from '#lib/format.js';
	import Meter from '#lib/components/Meter.svelte';
	import StatusBadge from '#lib/components/StatusBadge.svelte';
	import { getJson } from '#lib/api.js';
	import type { NodeStatus } from '#lib/types.js';

	const nodes = $derived(live.visible.flatMap((c) => c.nodes));
	const multi = $derived(live.visible.length > 1);

	// Maintenance, updates, reboot-required, temperature (refreshed every 30 s).
	let extra = $state<Record<string, NodeStatus>>({});
	$effect(() => {
		const load = () =>
			getJson<NodeStatus[]>('/api/v1/node-status')
				.then((rows) => (extra = Object.fromEntries(rows.map((r) => [`${r.cluster}/${r.node}`, r]))))
				.catch(() => {});
		load();
		const t = setInterval(load, 30_000);
		return () => clearInterval(t);
	});
</script>

<svelte:head><title>Nodes · Proxmox Dashboard</title></svelte:head>

<h1 class="mb-4 text-xl font-semibold">Nodes</h1>

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[1080px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr>
				<th class="px-4 py-3">Node</th>
				<th class="px-4 py-3">Status</th>
				<th class="w-40 px-4 py-3">CPU</th>
				<th class="w-48 px-4 py-3">RAM</th>
				<th class="w-32 px-4 py-3">Swap</th>
				<th class="px-4 py-3 text-right">IO-Delay</th>
				<th class="px-4 py-3 text-right">Load</th>
				<th class="px-4 py-3 text-right">Gäste</th>
				<th class="px-4 py-3">Uptime</th>
				<th class="px-4 py-3">Version</th>
				<th class="px-4 py-3">Pflege</th>
			</tr>
		</thead>
		<tbody>
			{#each nodes as n (n.cluster + n.node)}
				{@const x = extra[`${n.cluster}/${n.node}`]}
				<tr class="border-b border-zinc-100 last:border-0 hover:bg-zinc-50 dark:border-zinc-800/60 dark:hover:bg-zinc-800/40">
					<td class="px-4 py-3">
						<a href="/nodes/{n.cluster}/{n.node}" class="font-medium hover:text-accent">{n.node}</a>
						<div class="text-xs text-zinc-500">{multi ? `${n.cluster} · ` : ''}{n.ip ?? ''}</div>
					</td>
					<td class="px-4 py-3"><StatusBadge status={n.status} /></td>
					<td class="px-4 py-3"><Meter value={n.cpu} text="{pct(n.cpu)} von {n.maxcpu}" /></td>
					<td class="px-4 py-3">
						<Meter value={ratio(n.mem, n.maxmem)} text="{bytes(n.mem)} / {bytes(n.maxmem)}" />
					</td>
					<td class="px-4 py-3">
						<Meter value={ratio(n.swap_used, n.swap_total)} text={bytes(n.swap_used)} warn={0.3} crit={0.6} />
					</td>
					<td class="tabular px-4 py-3 text-right">{pct(n.iowait, 1)}</td>
					<td class="tabular px-4 py-3 text-right">{n.loadavg ? n.loadavg.map((l) => num(l, 2)).join(' / ') : '–'}</td>
					<td class="tabular px-4 py-3 text-right">{n.guests_running}/{n.guests_total}</td>
					<td class="tabular px-4 py-3">{uptime(n.uptime)}</td>
					<td class="px-4 py-3 text-xs">
						<div>PVE {n.pve_version ?? '–'}</div>
						<div class="text-zinc-500">{n.kernel ?? ''}</div>
					</td>
					<td class="px-4 py-3 text-xs">
						{#if x}
							<div class="flex flex-wrap gap-1">
								{#if x.maintenance}<span class="rounded bg-amber-500/15 px-1.5 text-amber-500">Wartung</span>{/if}
								{#if x.updates}<span class="rounded bg-blue-500/15 px-1.5 text-blue-500">{x.updates} Updates</span>{/if}
								{#if x.reboot_required}<span class="rounded bg-violet-500/15 px-1.5 text-violet-500">Neustart</span>{/if}
								{#if x.temperature != null}<span class={x.temperature >= 80 ? 'text-red-500' : 'text-zinc-500'}>{x.temperature.toFixed(0)} °C</span>{/if}
								{#if !x.maintenance && !x.updates && !x.reboot_required}<span class="text-emerald-500">✓</span>{/if}
							</div>
						{/if}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>
