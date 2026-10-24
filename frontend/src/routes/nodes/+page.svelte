<script lang="ts">
	import { live } from '#lib/live.svelte.js';
	import { bytes, num, pct, ratio, uptime } from '#lib/format.js';
	import Meter from '#lib/components/Meter.svelte';
	import StatusBadge from '#lib/components/StatusBadge.svelte';

	const nodes = $derived(live.visible.flatMap((c) => c.nodes));
	const multi = $derived(live.visible.length > 1);
</script>

<svelte:head><title>Nodes · Proxmox Dashboard</title></svelte:head>

<h1 class="mb-4 text-xl font-semibold">Nodes</h1>

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[960px] text-sm">
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
			</tr>
		</thead>
		<tbody>
			{#each nodes as n (n.cluster + n.node)}
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
				</tr>
			{/each}
		</tbody>
	</table>
</div>
