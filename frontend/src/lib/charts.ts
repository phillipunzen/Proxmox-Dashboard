// Shared ECharts option builders.
import type { EChartsCoreOption } from 'echarts/core';
import type { Point } from './live.svelte';
import { rate } from './format';

function isDark(): boolean {
	return typeof document !== 'undefined' && document.documentElement.classList.contains('dark');
}

function base(): EChartsCoreOption {
	const dark = isDark();
	const axis = dark ? '#71717a' : '#a1a1aa';
	const grid = dark ? '#27272a' : '#e4e4e7';
	return {
		backgroundColor: 'transparent',
		animationDuration: 300,
		textStyle: { color: axis },
		grid: { left: 48, right: 16, top: 28, bottom: 24 },
		legend: { top: 0, right: 0, textStyle: { color: axis }, icon: 'roundRect', itemHeight: 8 },
		tooltip: {
			trigger: 'axis',
			backgroundColor: dark ? '#18181b' : '#fff',
			borderColor: grid,
			textStyle: { color: dark ? '#e4e4e7' : '#27272a' }
		},
		xAxis: {
			type: 'time',
			axisLine: { lineStyle: { color: grid } },
			splitLine: { show: false },
			axisLabel: { color: axis, hideOverlap: true }
		}
	};
}

const lineSeries = (name: string, color: string, data: [number, number][]) => ({
	name,
	type: 'line',
	data,
	showSymbol: false,
	smooth: 0.2,
	lineStyle: { width: 1.5, color },
	itemStyle: { color },
	areaStyle: { color, opacity: 0.08 }
});

/** CPU + RAM utilisation (0..100 %). */
export function utilisationOption(points: Point[]): EChartsCoreOption {
	const grid = isDark() ? '#27272a' : '#e4e4e7';
	return {
		...base(),
		yAxis: {
			type: 'value',
			min: 0,
			max: 100,
			axisLabel: { formatter: '{value} %' },
			splitLine: { lineStyle: { color: grid } }
		},
		tooltip: {
			...(base().tooltip as object),
			valueFormatter: (v: number) => `${v.toFixed(1)} %`
		},
		series: [
			lineSeries('CPU', '#e57000', points.map((p) => [p.t, p.cpu * 100])),
			lineSeries('RAM', '#3b82f6', points.map((p) => [p.t, p.mem * 100]))
		]
	};
}

/** Network throughput in bytes/s. */
export function networkOption(points: Point[]): EChartsCoreOption {
	const withRates = points.filter((p) => p.netin != null && p.netout != null);
	const grid = isDark() ? '#27272a' : '#e4e4e7';
	return {
		...base(),
		yAxis: {
			type: 'value',
			min: 0,
			axisLabel: { formatter: (v: number) => rate(v) },
			splitLine: { lineStyle: { color: grid } }
		},
		grid: { left: 80, right: 16, top: 28, bottom: 24 },
		tooltip: { ...(base().tooltip as object), valueFormatter: (v: number) => rate(v) },
		series: [
			lineSeries('Eingang', '#10b981', withRates.map((p) => [p.t, p.netin!])),
			lineSeries('Ausgang', '#a855f7', withRates.map((p) => [p.t, p.netout!]))
		]
	};
}
