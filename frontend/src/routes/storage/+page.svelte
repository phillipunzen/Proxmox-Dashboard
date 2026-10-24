<script lang="ts">
	import { getJson } from '#lib/api.js';
	import { live } from '#lib/live.svelte.js';
	import { bytes, pct, ratio } from '#lib/format.js';
	import { timeSeriesOption, PALETTE } from '#lib/charts.js';
	import Chart from '#lib/components/Chart.svelte';
	import Meter from '#lib/components/Meter.svelte';
	import RangePicker from '#lib/components/RangePicker.svelte';
	import type { Pt, RangeKey, StorageForecast } from '#lib/types.js';

	interface StorageSeries {
		cluster: string;
		node: string;
		storage: string;
		type: string;
		used: Pt[];
		total: number | null;
	}

	let forecasts = $state<StorageForecast[]>([]);
	let series = $state<StorageSeries[]>([]);
	let range = $state<RangeKey>('30d');
	let error = $state<string | null>(null);

	$effect(() => {
		const cluster = live.selected ?? undefined;
		getJson<StorageForecast[]>('/api/v1/storage/forecast', { cluster })
			.then((f) => (forecasts = f))
			.catch((e) => (error = e.message));
	});

	$effect(() => {
		if (!live.meta?.history) return;
		getJson<StorageSeries[]>('/api/v1/history/storage', { cluster: live.selected ?? undefined, range })
			.then((s) => (series = s))
			.catch(() => (series = []));
	});

	const label = (f: { node: string; storage: string }) => (f.node === 'shared' ? f.storage : `${f.storage} (${f.node})`);
	const days = (d: number | null) =>
		d == null ? '–' : d < 1 ? '< 1 Tag' : d > 3650 ? '> 10 Jahre' : `${Math.round(d)} Tage`;
	const urgency = (d: number | null) =>
		d == null ? 'text-zinc-500' : d < 14 ? 'text-red-500 font-semibold' : d < 60 ? 'text-amber-500' : 'text-emerald-500';

	// Usage in % so differently sized storages fit into one chart.
	const chart = $derived(
		timeSeriesOption(
			series.filter((s) => s.total).map((s, i) => ({
				name: label(s),
				color: PALETTE[i % PALETTE.length],
				data: s.used.map(([t, v]) => [t, v / (s.total ?? 1)] as [number, number])
			})),
			{ unit: 'pct', max: 1 }
		)
	);
</script>

<svelte:head><title>Storage · Proxmox Dashboard</title></svelte:head>

<h1 class="mb-4 text-xl font-semibold">Storage & Prognose</h1>

{#if live.meta && !live.meta.history}
	<div class="card mb-4 text-sm text-zinc-500">Trend und Prognose brauchen die Zeitreihen-Datenbank (VM_URL).</div>
{/if}
{#if error}<div class="card mb-4 text-sm text-red-500">{error}</div>{/if}

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[820px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr><th class="px-4 py-3">Storage</th><th class="px-4 py-3">Typ</th><th class="w-64 px-4 py-3">Belegung</th><th class="px-4 py-3 text-right">Trend / Tag</th><th class="px-4 py-3 text-right">90 % in</th><th class="px-4 py-3 text-right">Voll in</th></tr>
		</thead>
		<tbody>
			{#each forecasts as f (f.cluster + f.node + f.storage)}
				<tr class="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60">
					<td class="px-4 py-2 font-medium">{label(f)}{#if live.visible.length > 1}<div class="text-xs text-zinc-500">{f.cluster}</div>{/if}</td>
					<td class="px-4 py-2 text-xs text-zinc-500">{f.type}</td>
					<td class="px-4 py-2"><Meter value={ratio(f.used, f.total)} text="{bytes(f.used)} / {bytes(f.total)} ({pct(ratio(f.used, f.total))})" warn={0.85} crit={0.95} /></td>
					<td class="tabular px-4 py-2 text-right">{f.growth_per_day == null ? '–' : `${f.growth_per_day >= 0 ? '+' : '−'}${bytes(Math.abs(f.growth_per_day))}`}</td>
					<td class="tabular px-4 py-2 text-right">{days(f.days_until_90)}</td>
					<td class="tabular px-4 py-2 text-right {urgency(f.days_until_full)}">{f.days_until_full == null ? 'wächst nicht' : days(f.days_until_full)}</td>
				</tr>
			{:else}
				<tr><td colspan="6" class="px-4 py-6 text-center text-zinc-500">Noch keine Daten (Prognose braucht einige Stunden Verlauf).</td></tr>
			{/each}
		</tbody>
	</table>
</div>
<p class="mt-2 text-xs text-zinc-500">Lineare Regression über die letzten 14 Tage. Einmalige Sprünge (große Uploads, Löschungen) verzerren die Prognose – sie ist ein Frühwarnsignal, keine exakte Vorhersage.</p>

{#if live.meta?.history}
	<div class="card mt-4">
		<div class="mb-3 flex items-center justify-between"><span class="label">Belegung im Verlauf</span><RangePicker bind:value={range} /></div>
		<Chart option={chart} height="320px" />
	</div>
{/if}
