<script lang="ts">
	import { page } from '$app/state';
	import { live } from '#lib/live.svelte.js';
	import { bytes, num, pct, rate, ratio, uptime } from '#lib/format.js';
	import { utilisationOption } from '#lib/charts.js';
	import Chart from '#lib/components/Chart.svelte';
	import Kpi from '#lib/components/Kpi.svelte';
	import StatusBadge from '#lib/components/StatusBadge.svelte';

	const cluster = $derived(live.clusters.find((c) => c.name === page.params.cluster));
	const node = $derived(cluster?.nodes.find((n) => n.node === page.params.node));
	const guests = $derived(
		(cluster?.guests ?? [])
			.filter((g) => g.node === page.params.node && !g.template)
			.sort((a, b) => b.cpu_host - a.cpu_host)
	);
	const storages = $derived(
		(cluster?.storages ?? []).filter((s) => s.node === page.params.node)
	);
	const hist = $derived(live.history[`node:${page.params.cluster}/${page.params.node}`] ?? []);
</script>

<svelte:head><title>{page.params.node} · Proxmox Dashboard</title></svelte:head>

<a href="/nodes" class="text-sm text-zinc-500 hover:text-accent">← Nodes</a>

{#if !node}
	<div class="card mt-4 text-sm text-zinc-500">
		{live.clusters.length ? 'Node nicht gefunden.' : 'Lade …'}
	</div>
{:else}
	<div class="mt-2 mb-4 flex flex-wrap items-center gap-3">
		<h1 class="text-xl font-semibold">{node.node}</h1>
		<StatusBadge status={node.status} />
		<span class="text-sm text-zinc-500">{node.cluster} · {node.ip ?? ''}</span>
	</div>

	<div class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
		<Kpi label="CPU" value={pct(node.cpu)} sub="{node.maxcpu} Threads" ratio={node.cpu} />
		<Kpi label="RAM" value={pct(ratio(node.mem, node.maxmem))} sub="{bytes(node.mem)} / {bytes(node.maxmem)}" ratio={ratio(node.mem, node.maxmem)} />
		<Kpi label="Swap" value={bytes(node.swap_used)} sub="von {bytes(node.swap_total)}" ratio={ratio(node.swap_used, node.swap_total)} warn={0.3} crit={0.6} />
		<Kpi label="Root-FS" value={pct(ratio(node.rootfs_used, node.rootfs_total))} sub="{bytes(node.rootfs_used)} / {bytes(node.rootfs_total)}" ratio={ratio(node.rootfs_used, node.rootfs_total)} />
		<Kpi label="IO-Delay" value={pct(node.iowait, 1)} ratio={node.iowait} warn={0.05} crit={0.1} />
		<Kpi label="Load" value={num(node.loadavg?.[0], 2)} sub={node.loadavg ? `5m ${num(node.loadavg[1])} · 15m ${num(node.loadavg[2])}` : ''} />
	</div>

	<div class="mt-4 grid gap-4 lg:grid-cols-3">
		<div class="card lg:col-span-2">
			<span class="label">Auslastung live</span>
			<Chart option={utilisationOption(hist)} height="220px" />
		</div>
		<div class="card text-sm">
			<span class="label">System</span>
			<dl class="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
				<dt class="text-zinc-500">CPU</dt><dd>{node.cpu_model ?? '–'}</dd>
				<dt class="text-zinc-500">Topologie</dt>
				<dd>{node.cpu_sockets ?? '–'} Socket · {node.cpu_cores ?? '–'} Kerne · {node.maxcpu} Threads</dd>
				<dt class="text-zinc-500">PVE</dt><dd>{node.pve_version ?? '–'}</dd>
				<dt class="text-zinc-500">Kernel</dt><dd class="break-all">{node.kernel ?? '–'}</dd>
				<dt class="text-zinc-500">Uptime</dt><dd>{uptime(node.uptime)}</dd>
				<dt class="text-zinc-500">vCPU zugewiesen</dt>
				<dd>{node.vcpus_allocated} ({num(ratio(node.vcpus_allocated, node.maxcpu), 1)}×)</dd>
				<dt class="text-zinc-500">RAM zugewiesen</dt>
				<dd>{bytes(node.mem_allocated)} ({pct(ratio(node.mem_allocated, node.maxmem))})</dd>
			</dl>
		</div>
	</div>

	<div class="mt-4 grid gap-4 lg:grid-cols-3">
		<div class="card overflow-x-auto p-0 lg:col-span-2">
			<div class="label px-4 pt-4">Gäste auf {node.node} (nach Host-CPU-Anteil)</div>
			<table class="mt-2 w-full min-w-[560px] text-sm">
				<tbody>
					{#each guests as g (g.vmid)}
						<tr class="border-t border-zinc-100 dark:border-zinc-800/60">
							<td class="px-4 py-2"><span class="text-zinc-500">{g.vmid}</span> {g.name}</td>
							<td class="px-4 py-2 text-xs uppercase text-zinc-500">{g.type === 'qemu' ? 'VM' : 'LXC'}</td>
							<td class="px-4 py-2"><StatusBadge status={g.status} /></td>
							<td class="tabular px-4 py-2 text-right">{pct(g.cpu_host, 1)}</td>
							<td class="tabular px-4 py-2 text-right">{bytes(g.mem)}</td>
							<td class="tabular px-4 py-2 text-right">{rate((g.netin_bps ?? 0) + (g.netout_bps ?? 0))}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
		<div class="card">
			<span class="label">Storage</span>
			<ul class="mt-3 flex flex-col gap-3 text-sm">
				{#each storages as s (s.storage)}
					<li>
						<div class="flex justify-between gap-2">
							<span>{s.storage} <span class="text-xs text-zinc-500">{s.type}{s.shared ? ' · shared' : ''}</span></span>
							<span class="tabular text-xs">{pct(ratio(s.used, s.total))}</span>
						</div>
						<div class="text-xs text-zinc-500">{bytes(s.used)} / {bytes(s.total)}</div>
					</li>
				{/each}
			</ul>
		</div>
	</div>
{/if}
