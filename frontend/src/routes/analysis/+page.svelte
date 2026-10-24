<script lang="ts">
	// Load analysis: "Where does the load come from?"
	// Stacked contribution of the top guests to the host load, click on the chart for a
	// drill-down at that point in time, top consumers over the range and overcommitment.
	import { page } from '$app/state';
	import { getJson } from '#lib/api.js';
	import { live } from '#lib/live.svelte.js';
	import { dateTime, num, pct, unitFormat, type Unit } from '#lib/format.js';
	import { OTHER_COLOR, PALETTE, timeSeriesOption, treemapOption, type LineDef } from '#lib/charts.js';
	import Chart from '#lib/components/Chart.svelte';
	import Meter from '#lib/components/Meter.svelte';
	import RangePicker from '#lib/components/RangePicker.svelte';
	import type {
		AnalysisMetric,
		Contribution,
		NodeAllocation,
		RangeKey,
		Ranking,
		TopGuest
	} from '#lib/types.js';

	const metrics: { key: AnalysisMetric; label: string }[] = [
		{ key: 'cpu', label: 'CPU' },
		{ key: 'mem', label: 'RAM' },
		{ key: 'diskread', label: 'Disk lesen' },
		{ key: 'diskwrite', label: 'Disk schreiben' },
		{ key: 'netin', label: 'Netz ein' },
		{ key: 'netout', label: 'Netz aus' }
	];

	const qp = page.url.searchParams;
	let cluster = $state(qp.get('cluster') ?? '');
	let node = $state(qp.get('node') ?? '');
	let metric = $state<AnalysisMetric>((qp.get('metric') as AnalysisMetric) ?? 'cpu');
	let range = $state<RangeKey>((qp.get('range') as RangeKey) ?? '24h');
	let type = $state('');
	let tag = $state('');
	let pool = $state('');
	let top = $state(8);

	let contribution = $state<Contribution | null>(null);
	let topList = $state<TopGuest[]>([]);
	let allocation = $state<NodeAllocation[]>([]);
	let ranking = $state<Ranking | null>(null);
	let selected = $state<number | null>(null);
	let error = $state<string | null>(null);
	let loading = $state(false);

	// Default to the first cluster once the live data is there.
	$effect(() => {
		if (!cluster && live.clusters.length) cluster = live.selected ?? live.clusters[0].name;
	});

	const snap = $derived(live.clusters.find((c) => c.name === cluster));
	const nodes = $derived(snap?.nodes.map((n) => n.node) ?? []);
	const tags = $derived([...new Set(snap?.guests.flatMap((g) => g.tags) ?? [])].sort());
	const pools = $derived([...new Set(snap?.guests.map((g) => g.pool).filter(Boolean) as string[])].sort());
	const unit = $derived<Unit>(contribution?.unit ?? 'cores');
	const filters = $derived({
		cluster,
		node: node || undefined,
		metric,
		range,
		type: type || undefined,
		tag: tag ? [tag] : undefined,
		pool: pool || undefined
	});

	$effect(() => {
		if (!cluster) return;
		const ctl = new AbortController();
		const params = { ...filters };
		loading = true;
		selected = null;
		ranking = null;
		Promise.all([
			getJson<Contribution>('/api/v1/analysis/contribution', { ...params, top }, ctl.signal),
			getJson<TopGuest[]>('/api/v1/analysis/top', { ...params, limit: 20 }, ctl.signal)
		])
			.then(([c, t]) => {
				contribution = c;
				topList = t;
				error = null;
			})
			.catch((e) => {
				if (e.name !== 'AbortError') error = e.message;
			})
			.finally(() => (loading = false));
		return () => ctl.abort();
	});

	$effect(() => {
		if (!cluster) return;
		getJson<NodeAllocation[]>('/api/v1/analysis/allocation', { cluster })
			.then((a) => (allocation = a))
			.catch(() => (allocation = []));
	});

	async function drillDown(seconds: number) {
		if (!contribution) return;
		// Snap to the nearest chart point (points are aligned to the step).
		const step = contribution.step;
		const at = Math.round(seconds / step) * step;
		selected = at;
		try {
			ranking = await getJson<Ranking>('/api/v1/analysis/at', { ...filters, at, window: step });
		} catch (e) {
			error = (e as Error).message;
		}
	}

	// Stable colours per guest: chart order first, then the remaining top-list entries.
	const colors = $derived.by(() => {
		const map = new Map<number, string>();
		for (const g of contribution?.guests ?? []) map.set(g.vmid, PALETTE[map.size % PALETTE.length]);
		for (const g of topList) if (!map.has(g.vmid)) map.set(g.vmid, PALETTE[map.size % PALETTE.length]);
		return map;
	});
	const colorOf = (vmid: number) => colors.get(vmid) ?? OTHER_COLOR;

	const chartOption = $derived.by(() => {
		if (!contribution) return timeSeriesOption([], { unit });
		const lines: LineDef[] = contribution.guests.map((g) => ({
			name: `${g.vmid} ${g.name}`,
			data: g.points,
			stack: 'guests',
			color: colorOf(g.vmid)
		}));
		if (contribution.other_count) {
			lines.push({
				name: `Andere (${contribution.other_count})`,
				data: contribution.other,
				stack: 'guests',
				color: OTHER_COLOR
			});
		}
		lines.push({
			name: metric === 'cpu' || metric === 'mem' ? 'Host gesamt' : 'Summe aller Gäste',
			data: contribution.total,
			color: document.documentElement.classList.contains('dark') ? '#f4f4f5' : '#18181b'
		});
		if (contribution.capacity?.length) {
			lines.push({ name: 'Kapazität', data: contribution.capacity, color: '#ef4444', dashed: true });
		}
		return timeSeriesOption(lines, { unit, marker: selected });
	});

	const treemap = $derived(
		treemapOption(
			topList.filter((g) => g.avg > 0).map((g) => ({ name: `${g.vmid} ${g.name}`, value: g.avg, color: colorOf(g.vmid) })),
			unit
		)
	);

	const ratioLabel = (r: number | null) => (r == null ? '–' : `${num(r, 2)}×`);
</script>

<svelte:head><title>Lastanalyse · Proxmox Dashboard</title></svelte:head>

<div class="mb-4 flex flex-wrap items-center gap-2">
	<h1 class="mr-auto text-xl font-semibold">Lastanalyse</h1>
	{#if live.clusters.length > 1}
		<select class="input" bind:value={cluster} aria-label="Cluster">
			{#each live.clusters as c (c.name)}<option value={c.name}>{c.name}</option>{/each}
		</select>
	{/if}
	<select class="input" bind:value={node} aria-label="Node">
		<option value="">Ganzer Cluster</option>
		{#each nodes as n (n)}<option value={n}>{n}</option>{/each}
	</select>
	<select class="input" bind:value={type} aria-label="Typ">
		<option value="">VM + LXC</option>
		<option value="qemu">nur VMs</option>
		<option value="lxc">nur LXC</option>
	</select>
	{#if tags.length}
		<select class="input" bind:value={tag} aria-label="Tag">
			<option value="">Alle Tags</option>
			{#each tags as t (t)}<option value={t}>{t}</option>{/each}
		</select>
	{/if}
	{#if pools.length}
		<select class="input" bind:value={pool} aria-label="Pool">
			<option value="">Alle Pools</option>
			{#each pools as p (p)}<option value={p}>{p}</option>{/each}
		</select>
	{/if}
	<RangePicker bind:value={range} />
</div>

{#if live.meta && !live.meta.history}
	<div class="card text-sm text-zinc-500">
		Die Lastanalyse braucht die Zeitreihen-Datenbank. Setze <code>VM_URL</code> (siehe README).
	</div>
{:else}
	<div class="mb-3 flex flex-wrap gap-1" role="tablist" aria-label="Metrik">
		{#each metrics as m (m.key)}
			<button
				role="tab"
				aria-selected={metric === m.key}
				class="rounded-lg px-3 py-1.5 text-sm font-medium {metric === m.key
					? 'bg-accent-soft text-accent'
					: 'text-zinc-500 hover:bg-zinc-100 dark:hover:bg-zinc-800'}"
				onclick={() => (metric = m.key)}>{m.label}</button
			>
		{/each}
	</div>

	{#if error}
		<div class="mb-3 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-600 dark:text-red-400">{error}</div>
	{/if}

	<div class="grid gap-4 xl:grid-cols-3">
		<div class="card xl:col-span-2" class:opacity-60={loading}>
			<div class="mb-2 flex flex-wrap items-baseline justify-between gap-2">
				<span class="label">Anteil der Gäste an der Last {node ? `auf ${node}` : 'im Cluster'}</span>
				<span class="text-xs text-zinc-500">Klick in das Diagramm → wer war zu dem Zeitpunkt verantwortlich?</span>
			</div>
			<Chart option={chartOption} height="380px" ontimeclick={drillDown} />
			{#if metric === 'cpu'}
				<p class="mt-2 text-xs text-zinc-500">
					Einheit: belegte Host-Threads. Ein Gast mit 50 % Last auf 4 vCPUs belegt 2 Threads. Die Lücke zwischen
					den Gästen und „Host gesamt“ ist Last des Hosts selbst (ZFS, Backups, Kernel).
				</p>
			{/if}
		</div>

		<!-- Drill-down -->
		<div class="card">
			<span class="label">Drill-down</span>
			{#if !ranking}
				<p class="mt-3 text-sm text-zinc-500">Klicke auf eine Stelle im Diagramm, z. B. eine Lastspitze.</p>
			{:else}
				<div class="mt-2 text-sm">
					<div class="font-medium">{dateTime(ranking.at)}</div>
					<div class="text-xs text-zinc-500">
						Mittel über {ranking.window} s · gesamt {unitFormat(ranking.total, unit)}
						{#if ranking.capacity}von {unitFormat(ranking.capacity, unit)}{/if}
					</div>
				</div>
				<ul class="mt-3 flex max-h-[420px] flex-col gap-2.5 overflow-y-auto pr-1">
					{#each ranking.guests.filter((g) => g.value > 0) as g (g.cluster + g.vmid + g.node)}
						<li>
							<a href="/guests/{g.cluster}/{g.vmid}" class="block hover:text-accent">
								<Meter
									label="{g.vmid} · {g.name}{g.node && !node ? ` (${g.node})` : ''}"
									value={g.share_of_total ?? 0}
									text="{unitFormat(g.value, unit)} · {pct(g.share_of_total, 0)}"
									warn={0.3}
									crit={0.5}
								/>
							</a>
						</li>
					{:else}
						<li class="text-sm text-zinc-500">Keine Daten zu diesem Zeitpunkt.</li>
					{/each}
				</ul>
			{/if}
		</div>
	</div>

	<div class="mt-4 grid gap-4 xl:grid-cols-3">
		<!-- Top table -->
		<div class="card overflow-x-auto p-0 xl:col-span-2">
			<div class="label px-4 pt-4">Top-Verbraucher im Zeitraum ({range})</div>
			<table class="mt-2 w-full min-w-[560px] text-sm">
				<thead class="text-left text-xs text-zinc-500">
					<tr>
						<th class="px-4 py-2">Gast</th>
						<th class="px-4 py-2">Node</th>
						<th class="px-4 py-2 text-right">Ø</th>
						<th class="px-4 py-2 text-right">p95</th>
						<th class="px-4 py-2 text-right">Max</th>
					</tr>
				</thead>
				<tbody>
					{#each topList as g (g.cluster + g.vmid)}
						<tr class="border-t border-zinc-100 dark:border-zinc-800/60">
							<td class="px-4 py-2">
								<span class="mr-1 inline-block size-2 rounded-full" style:background={colorOf(g.vmid)}></span>
								<a href="/guests/{g.cluster}/{g.vmid}" class="hover:text-accent"><span class="text-zinc-500">{g.vmid}</span> {g.name}</a>
								<span class="ml-1 text-[10px] text-zinc-500 uppercase">{g.type === 'qemu' ? 'VM' : 'LXC'}</span>
							</td>
							<td class="px-4 py-2 text-zinc-500">{g.node ?? '–'}</td>
							<td class="tabular px-4 py-2 text-right">{unitFormat(g.avg, unit)}</td>
							<td class="tabular px-4 py-2 text-right">{unitFormat(g.p95, unit)}</td>
							<td class="tabular px-4 py-2 text-right">{unitFormat(g.max, unit)}</td>
						</tr>
					{:else}
						<tr><td colspan="5" class="px-4 py-6 text-center text-zinc-500">Keine Daten.</td></tr>
					{/each}
				</tbody>
			</table>
		</div>
		<div class="card">
			<span class="label">Verteilung (Ø im Zeitraum)</span>
			<Chart option={treemap} height="320px" />
		</div>
	</div>
{/if}

<!-- Overcommitment works without history -->
<div class="card mt-4 overflow-x-auto p-0">
	<div class="label px-4 pt-4">Zuweisung vs. Hardware (Overcommitment)</div>
	<p class="px-4 pt-1 text-xs text-zinc-500">
		„Laufend“ zählt nur laufende Gäste. „Konfiguriert“ zählt auch gestoppte Gäste, also den Fall, dass alle gleichzeitig starten (z. B. nach einer Migration).
	</p>
	<table class="mt-2 w-full min-w-[760px] text-sm">
		<thead class="text-left text-xs text-zinc-500">
			<tr>
				<th class="px-4 py-2">Node</th>
				<th class="px-4 py-2 text-right">Threads</th>
				<th class="w-48 px-4 py-2">vCPU laufend</th>
				<th class="px-4 py-2 text-right">vCPU konfiguriert</th>
				<th class="w-48 px-4 py-2">RAM laufend</th>
				<th class="px-4 py-2 text-right">RAM konfiguriert</th>
			</tr>
		</thead>
		<tbody>
			{#each allocation as a (a.cluster + a.node)}
				<tr class="border-t border-zinc-100 dark:border-zinc-800/60">
					<td class="px-4 py-2">{a.node}{a.online ? '' : ' (offline)'}</td>
					<td class="tabular px-4 py-2 text-right">{a.threads}</td>
					<td class="px-4 py-2">
						<Meter value={(a.cpu_ratio_running ?? 0) / 4} text="{a.vcpus_running} · {ratioLabel(a.cpu_ratio_running)}" warn={0.5} crit={0.75} />
					</td>
					<td class="tabular px-4 py-2 text-right">{a.vcpus_configured} · {ratioLabel(a.cpu_ratio_configured)}</td>
					<td class="px-4 py-2">
						<Meter value={a.mem_ratio_running ?? 0} text="{unitFormat(a.mem_running, 'bytes')} · {pct(a.mem_ratio_running)}" warn={0.9} crit={1} />
					</td>
					<td class="tabular px-4 py-2 text-right {(a.mem_ratio_configured ?? 0) > 1 ? 'text-red-500' : ''}">
						{unitFormat(a.mem_configured, 'bytes')} · {pct(a.mem_ratio_configured)}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>
