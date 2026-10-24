<script lang="ts">
	// Loads a history SeriesSet and renders one chart per definition.
	// Short ranges refresh automatically so the charts keep moving.
	import { getJson } from '#lib/api.js';
	import { timeSeriesOption, PALETTE } from '#lib/charts.js';
	import type { Unit } from '#lib/format.js';
	import type { RangeKey, SeriesSet } from '#lib/types.js';
	import Chart from './Chart.svelte';

	export interface ChartDef {
		title: string;
		unit: Unit;
		max?: number;
		height?: string;
		lines: { key: string; name: string; color?: string }[];
	}

	let {
		path,
		range,
		charts,
		columns = 1
	}: { path: string; range: RangeKey; charts: ChartDef[]; columns?: 1 | 2 } = $props();

	let data = $state<SeriesSet | null>(null);
	let error = $state<string | null>(null);
	let loading = $state(false);

	async function load(signal?: AbortSignal) {
		loading = true;
		try {
			data = await getJson<SeriesSet>(path, { range }, signal);
			error = null;
		} catch (e) {
			if ((e as Error).name !== 'AbortError') error = (e as Error).message;
		} finally {
			loading = false;
		}
	}

	$effect(() => {
		const ctl = new AbortController();
		load(ctl.signal);
		const refresh = range === '1h' || range === '6h' ? 30_000 : 300_000;
		const timer = setInterval(() => load(), refresh);
		return () => {
			ctl.abort();
			clearInterval(timer);
		};
	});
</script>

{#if error}
	<div class="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-600 dark:text-amber-400">
		Verlauf nicht verfügbar: {error}
	</div>
{:else}
	<div class="grid gap-4 {columns === 2 ? 'lg:grid-cols-2' : ''}" class:opacity-60={loading && !data}>
		{#each charts as c (c.title)}
			<div>
				<div class="label mb-1">{c.title}</div>
				<Chart
					height={c.height ?? '200px'}
					option={timeSeriesOption(
						c.lines.map((l, i) => ({
							name: l.name,
							color: l.color ?? PALETTE[i],
							area: true,
							data: data?.series[l.key] ?? []
						})),
						{ unit: c.unit, max: c.max, legend: c.lines.length > 1 }
					)}
				/>
			</div>
		{/each}
	</div>
{/if}
