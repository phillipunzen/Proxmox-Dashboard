// Shared ECharts option builders. Timestamps from the API are unix seconds.
import type { EChartsCoreOption } from 'echarts/core';
import type { Point } from './live.svelte';
import { unitFormat, type Unit } from './format';
import type { Pt } from './types';

// Categorical palette: distinguishable in dark and light mode; "other" is neutral grey.
export const PALETTE = [
	'#e57000', '#3b82f6', '#10b981', '#a855f7', '#eab308',
	'#ec4899', '#14b8a6', '#f97316', '#6366f1', '#84cc16',
	'#06b6d4', '#f43f5e'
];
export const OTHER_COLOR = '#71717a';

function isDark(): boolean {
	return typeof document !== 'undefined' && document.documentElement.classList.contains('dark');
}

function theme() {
	const dark = isDark();
	return {
		axis: dark ? '#71717a' : '#a1a1aa',
		grid: dark ? '#27272a' : '#e4e4e7',
		tooltipBg: dark ? '#18181b' : '#fff',
		tooltipText: dark ? '#e4e4e7' : '#27272a'
	};
}

export interface LineDef {
	name: string;
	data: Pt[] | [number, number][];
	color?: string;
	area?: boolean;
	stack?: string;
	dashed?: boolean;
	/** Data is in milliseconds already (live points) instead of unix seconds. */
	ms?: boolean;
}

export interface TimeChartOptions {
	unit: Unit;
	min?: number;
	max?: number;
	/** Vertical marker (unix seconds), e.g. the drill-down selection. */
	marker?: number | null;
	legend?: boolean;
}

export function timeSeriesOption(lines: LineDef[], opts: TimeChartOptions): EChartsCoreOption {
	const t = theme();
	const fmt = (v: number) => unitFormat(opts.unit === 'pct' ? v / 100 : v, opts.unit);
	const scale = opts.unit === 'pct' ? 100 : 1;
	const series = lines.map((l, i) => {
		const color = l.color ?? PALETTE[i % PALETTE.length];
		return {
			name: l.name,
			type: 'line',
			data: (l.data as [number, number][]).map(([x, y]) => [l.ms ? x : x * 1000, y * scale]),
			showSymbol: false,
			smooth: 0.15,
			stack: l.stack,
			sampling: 'lttb',
			emphasis: { focus: l.stack ? 'series' : 'none' },
			lineStyle: { width: l.stack ? 0.8 : 1.5, color, type: l.dashed ? 'dashed' : 'solid' },
			itemStyle: { color },
			areaStyle: l.area || l.stack ? { color, opacity: l.stack ? 0.55 : 0.08 } : undefined,
			markLine:
				i === 0 && opts.marker
					? {
							symbol: 'none',
							silent: true,
							label: { show: false },
							lineStyle: { color: '#ef4444', width: 1.5, type: 'solid' },
							data: [{ xAxis: opts.marker * 1000 }]
						}
					: undefined
		};
	});
	return {
		backgroundColor: 'transparent',
		animation: false,
		textStyle: { color: t.axis },
		grid: { left: opts.unit === 'pct' ? 48 : 84, right: 16, top: opts.legend === false ? 12 : 32, bottom: 24 },
		legend:
			opts.legend === false
				? undefined
				: { top: 0, type: 'scroll', textStyle: { color: t.axis }, icon: 'roundRect', itemHeight: 8, pageTextStyle: { color: t.axis } },
		tooltip: {
			trigger: 'axis',
			confine: true,
			order: 'valueDesc',
			backgroundColor: t.tooltipBg,
			borderColor: t.grid,
			textStyle: { color: t.tooltipText, fontSize: 12 },
			valueFormatter: (v: number) => fmt(v)
		},
		xAxis: {
			type: 'time',
			axisLine: { lineStyle: { color: t.grid } },
			splitLine: { show: false },
			axisLabel: { color: t.axis, hideOverlap: true }
		},
		yAxis: {
			type: 'value',
			splitNumber: 4,
			min: opts.min != null ? opts.min * scale : 0,
			max: opts.max != null ? opts.max * scale : undefined,
			axisLabel: { color: t.axis, formatter: (v: number) => fmt(v) },
			splitLine: { lineStyle: { color: t.grid } }
		},
		series
	};
}

/** Treemap of guests by a value (e.g. average share over the range). */
export function treemapOption(
	items: { name: string; value: number; color?: string }[],
	unit: Unit
): EChartsCoreOption {
	const t = theme();
	return {
		backgroundColor: 'transparent',
		tooltip: {
			backgroundColor: t.tooltipBg,
			borderColor: t.grid,
			textStyle: { color: t.tooltipText },
			formatter: (p: { name: string; value: number }) => `${p.name}<br/>${unitFormat(p.value, unit)}`
		},
		series: [
			{
				type: 'treemap',
				roam: false,
				nodeClick: false,
				breadcrumb: { show: false },
				left: 0,
				top: 0,
				right: 0,
				bottom: 0,
				label: { color: '#fff', fontSize: 12, overflow: 'truncate' },
				itemStyle: { borderColor: isDark() ? '#18181b' : '#fff', borderWidth: 2, gapWidth: 2 },
				data: items.map((it, i) => ({
					name: it.name,
					value: it.value,
					itemStyle: { color: it.color ?? PALETTE[i % PALETTE.length] }
				}))
			}
		]
	};
}

// --- live (browser-side) fallbacks when no TSDB is configured ---------------------------------

export function utilisationOption(points: Point[]): EChartsCoreOption {
	return timeSeriesOption(
		[
			{ name: 'CPU', ms: true, area: true, color: PALETTE[0], data: points.map((p) => [p.t, p.cpu]) },
			{ name: 'RAM', ms: true, area: true, color: PALETTE[1], data: points.map((p) => [p.t, p.mem]) }
		],
		{ unit: 'pct', max: 1 }
	);
}

export function networkOption(points: Point[]): EChartsCoreOption {
	const withRates = points.filter((p) => p.netin != null && p.netout != null);
	return timeSeriesOption(
		[
			{ name: 'Eingang', ms: true, area: true, color: PALETTE[2], data: withRates.map((p) => [p.t, p.netin!]) },
			{ name: 'Ausgang', ms: true, area: true, color: PALETTE[3], data: withRates.map((p) => [p.t, p.netout!]) }
		],
		{ unit: 'bps' }
	);
}
