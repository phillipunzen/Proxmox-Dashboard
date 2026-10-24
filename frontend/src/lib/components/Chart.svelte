<script lang="ts">
	// Thin ECharts wrapper: tree-shaken imports, theme-aware, resizes with its container.
	import { onMount } from 'svelte';
	import * as echarts from 'echarts/core';
	import { LineChart } from 'echarts/charts';
	import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
	import { CanvasRenderer } from 'echarts/renderers';
	import type { EChartsCoreOption } from 'echarts/core';

	echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

	let { option, height = '220px' }: { option: EChartsCoreOption; height?: string } = $props();

	let el: HTMLDivElement;
	let chart: echarts.ECharts | undefined;

	onMount(() => {
		chart = echarts.init(el, null, { renderer: 'canvas' });
		chart.setOption(option);
		const ro = new ResizeObserver(() => chart?.resize());
		ro.observe(el);
		return () => {
			ro.disconnect();
			chart?.dispose();
		};
	});

	$effect(() => {
		// Merge updates so animations stay smooth when new points arrive.
		chart?.setOption(option, { lazyUpdate: true });
	});
</script>

<div bind:this={el} style:height class="w-full"></div>
