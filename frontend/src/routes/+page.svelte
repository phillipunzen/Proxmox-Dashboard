<script lang="ts">
	import { live } from '#lib/live.svelte.js';
	import { bytes, num, pct, rate, ratio, uptime } from '#lib/format.js';
	import { networkOption, utilisationOption } from '#lib/charts.js';
	import Chart from '#lib/components/Chart.svelte';
	import HistoryCharts from '#lib/components/HistoryCharts.svelte';
	import RangePicker from '#lib/components/RangePicker.svelte';
	import type { RangeKey } from '#lib/types.js';
	import Kpi from '#lib/components/Kpi.svelte';
	import Meter from '#lib/components/Meter.svelte';
	import StatusBadge from '#lib/components/StatusBadge.svelte';

	const severityOrder = { critical: 0, warning: 1, info: 2 } as const;
	let range = $state<RangeKey>('1h');
</script>

<svelte:head><title>Übersicht · Proxmox Dashboard</title></svelte:head>

{#if live.clusters.length === 0}
	<div class="card text-sm text-zinc-500">Warte auf Daten vom Backend …</div>
{/if}

<div class="flex flex-col gap-10">
	{#each live.visible as c (c.name)}
		{@const t = c.totals}
		{@const hist = live.history[`cluster:${c.name}`] ?? []}
		{@const topGuests = [...c.guests]
			.filter((g) => g.status === 'running')
			.sort((a, b) => b.cpu_host - a.cpu_host)
			.slice(0, 5)}
		<section class="flex flex-col gap-4">
			<!-- Cluster header -->
			<div class="flex flex-wrap items-center gap-x-3 gap-y-2">
				<h1 class="text-xl font-semibold">{c.name}</h1>
				<StatusBadge
					status={c.reachable ? 'ok' : 'critical'}
					label={c.reachable ? 'erreichbar' : 'nicht erreichbar'}
				/>
				{#if c.is_cluster}
					<StatusBadge
						status={c.quorate ? 'ok' : 'critical'}
						label={c.quorate ? 'Quorum OK' : 'kein Quorum'}
					/>
				{:else}
					<span class="text-xs text-zinc-500">Standalone-Host</span>
				{/if}
				<span class="text-sm text-zinc-500">
					{t.nodes_online}/{t.nodes_total} Nodes online
					{#if c.pve_version}· PVE {c.pve_version}{/if}
				</span>
				{#if c.ui_url}
					<a
						href={c.ui_url}
						target="_blank"
						rel="noopener noreferrer"
						class="ml-auto text-sm text-accent hover:underline">Proxmox-Oberfläche ↗</a
					>
				{/if}
			</div>
			{#if c.last_error}
				<div class="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-600 dark:text-red-400">
					Letzter Fehler: {c.last_error}
				</div>
			{/if}

			<!-- KPIs -->
			<div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
				<Kpi label="CPU" value={pct(t.cpu)} sub="{t.cpus} Threads · Load {num(t.load1, 1)}" ratio={t.cpu} />
				<Kpi
					label="RAM"
					value={pct(ratio(t.mem, t.maxmem))}
					sub="{bytes(t.mem)} / {bytes(t.maxmem)}"
					ratio={ratio(t.mem, t.maxmem)}
				/>
				<Kpi
					label="Swap"
					value={pct(ratio(t.swap_used, t.swap_total))}
					sub="{bytes(t.swap_used)} / {bytes(t.swap_total)}"
					ratio={ratio(t.swap_used, t.swap_total)}
					warn={0.3}
					crit={0.6}
				/>
				<Kpi
					label="Storage"
					value={pct(ratio(t.storage_used, t.storage_total))}
					sub="{bytes(t.storage_used)} / {bytes(t.storage_total)}"
					ratio={ratio(t.storage_used, t.storage_total)}
					warn={0.85}
					crit={0.95}
				/>
				<Kpi label="IO-Delay" value={pct(t.iowait, 1)} sub="Mittel der Nodes" ratio={t.iowait} warn={0.05} crit={0.1} />
				<Kpi label="Netzwerk" value={rate(t.netin_bps + t.netout_bps)} sub="↓ {rate(t.netin_bps)} · ↑ {rate(t.netout_bps)}" />
				<Kpi label="Disk-IO" value={rate(t.diskread_bps + t.diskwrite_bps)} sub="R {rate(t.diskread_bps)} · W {rate(t.diskwrite_bps)}" />
				<Kpi
					label="Gäste"
					value="{t.vms_running + t.lxc_running} / {t.vms_running + t.vms_stopped + t.lxc_running + t.lxc_stopped}"
					sub="VM {t.vms_running}/{t.vms_running + t.vms_stopped} · LXC {t.lxc_running}/{t.lxc_running + t.lxc_stopped}"
				/>
			</div>

			<div class="grid gap-4 lg:grid-cols-3">
				<!-- Live charts -->
				<div class="card lg:col-span-2">
					{#if live.meta?.history}
						<div class="mb-3 flex items-center justify-between gap-2">
							<span class="label">Verlauf</span>
							<RangePicker bind:value={range} />
						</div>
						<HistoryCharts
							path="/api/v1/history/clusters/{encodeURIComponent(c.name)}"
							{range}
							charts={[
								{ title: 'CPU / RAM', unit: 'pct', max: 1, lines: [{ key: 'cpu', name: 'CPU' }, { key: 'mem', name: 'RAM' }] },
								{ title: 'Netzwerk (Summe Gäste)', unit: 'bps', height: '160px', lines: [{ key: 'netin', name: 'Eingang', color: '#10b981' }, { key: 'netout', name: 'Ausgang', color: '#a855f7' }] }
							]}
						/>
					{:else}
						<div class="mb-2 flex items-center justify-between">
							<span class="label">Auslastung live</span>
							<span class="text-xs text-zinc-500">seit Seitenaufruf · ohne VM_URL keine Historie</span>
						</div>
						<Chart option={utilisationOption(hist)} height="200px" />
						<Chart option={networkOption(hist)} height="160px" />
					{/if}
				</div>

				<div class="flex flex-col gap-4">
					<!-- Overcommitment -->
					<div class="card flex flex-col gap-3">
						<span class="label">Zuweisung laufender Gäste</span>
						<Meter
							label="vCPU / Threads"
							value={ratio(t.vcpus_allocated, t.cpus) / 4}
							text="{t.vcpus_allocated} / {t.cpus} ({num(ratio(t.vcpus_allocated, t.cpus), 1)}×)"
							warn={0.5}
							crit={0.75}
						/>
						<Meter
							label="RAM zugewiesen / physisch"
							value={ratio(t.mem_allocated, t.maxmem)}
							text="{bytes(t.mem_allocated)} / {bytes(t.maxmem)}"
							warn={0.9}
							crit={1}
						/>
						<p class="text-xs text-zinc-500">
							vCPU-Balken: voll bei 4× Überbuchung. RAM über 100 % bedeutet Overcommit.
						</p>
					</div>

					<!-- Warnings -->
					<div class="card flex flex-col gap-2">
						<span class="label">Warnungen ({c.warnings.length})</span>
						{#each [...c.warnings].sort((a, b) => severityOrder[a.severity] - severityOrder[b.severity]) as w (w.target + w.message)}
							<div class="flex items-start gap-2 text-sm">
								<StatusBadge status={w.severity === 'info' ? 'unknown' : w.severity} label={w.severity === 'critical' ? 'kritisch' : w.severity === 'warning' ? 'Warnung' : 'Info'} />
								<span>{w.message}</span>
							</div>
						{:else}
							<span class="text-sm text-emerald-600 dark:text-emerald-400">Keine Warnungen</span>
						{/each}
					</div>
				</div>
			</div>

			<div class="grid gap-4 lg:grid-cols-3">
				<!-- Node cards -->
				<div class="grid content-start gap-3 sm:grid-cols-2 lg:col-span-2 xl:grid-cols-3">
					{#each c.nodes as n (n.node)}
						<a href="/nodes/{c.name}/{n.node}" class="card flex flex-col gap-3 transition-colors hover:border-accent/60">
							<div class="flex items-center justify-between gap-2">
								<span class="truncate font-medium">{n.node}</span>
								<StatusBadge status={n.status} />
							</div>
							{#if n.status === 'online'}
								<Meter label="CPU" value={n.cpu} text={pct(n.cpu)} />
								<Meter label="RAM" value={ratio(n.mem, n.maxmem)} text="{bytes(n.mem)} / {bytes(n.maxmem)}" />
								<div class="flex justify-between text-xs text-zinc-500">
									<span>{n.guests_running}/{n.guests_total} Gäste</span>
									<span>up {uptime(n.uptime)}</span>
								</div>
							{:else}
								<span class="text-sm text-zinc-500">Keine Daten</span>
							{/if}
						</a>
					{/each}
				</div>

				<!-- Top consumers -->
				<div class="card">
					<span class="label">Top 5 CPU (Anteil am Host)</span>
					<ul class="mt-3 flex flex-col gap-3">
						{#each topGuests as g (g.vmid)}
							<li>
								<a href="/guests/{g.cluster}/{g.vmid}" class="block hover:text-accent"><Meter
									label="{g.vmid} · {g.name} ({g.node})"
									value={g.cpu_host}
									text={pct(g.cpu_host, 1)}
									warn={0.25}
									crit={0.5}
								/></a>
							</li>
						{/each}
					</ul>
					<a href="/analysis?cluster={encodeURIComponent(c.name)}" class="mt-4 inline-block text-sm text-accent hover:underline">Lastanalyse →</a>
				</div>
			</div>
		</section>
	{/each}
</div>
