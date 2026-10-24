<script lang="ts">
	// Thin ECharts wrapper: tree-shaken imports, resizes with its container, optional click on
	// the time axis (used for the drill-down in the load analysis).
	import { onMount } from 'svelte';
	import * as echarts from 'echarts/core';
	import { LineChart, TreemapChart } from 'echarts/charts';
	import {
		GridComponent,
		LegendComponent,
		MarkLineComponent,
		TooltipComponent
	} from 'echarts/components';
	import { CanvasRenderer } from 'echarts/renderers';
	import type { EChartsCoreOption } from 'echarts/core';

	echarts.use([
		LineChart,
		TreemapChart,
		GridComponent,
		TooltipComponent,
		LegendComponent,
		MarkLineComponent,
		CanvasRenderer
	]);

	let {
		option,
		height = '220px',
		ontimeclick
	}: {
		option: EChartsCoreOption;
		height?: string;
		/** Called with the clicked time in unix seconds. */
		ontimeclick?: (seconds: number) => void;
	} = $props();

	let el: HTMLDivElement;
	let chart: echarts.ECharts | undefined;

	onMount(() => {
		chart = echarts.init(el, null, { renderer: 'canvas' });
		chart.setOption(option);
		const ro = new ResizeObserver(() => chart?.resize());
		ro.observe(el);
		// Clicks anywhere inside the plot area (not only on data points).
		chart.getZr().on('click', (ev) => {
			if (!ontimeclick || !chart) return;
			const point = [ev.offsetX, ev.offsetY];
			if (!chart.containPixel({ gridIndex: 0 }, point)) return;
			const [ms] = chart.convertFromPixel({ gridIndex: 0 }, point) as number[];
			ontimeclick(ms / 1000);
		});
		return () => {
			ro.disconnect();
			chart?.dispose();
		};
	});

	$effect(() => {
		// Replace series so stacked charts don't keep stale guests after a filter change.
		chart?.setOption(option, { notMerge: false, replaceMerge: ['series'], lazyUpdate: true });
	});
</script>

<div bind:this={el} style:height class="w-full" class:cursor-crosshair={!!ontimeclick}></div>
