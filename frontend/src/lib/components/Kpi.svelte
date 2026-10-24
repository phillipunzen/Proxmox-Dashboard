<script lang="ts">
	// Stat tile: label, big value, optional sub line and utilisation bar.
	import Meter from './Meter.svelte';

	let {
		label,
		value,
		sub = '',
		ratio = null,
		warn = 0.75,
		crit = 0.9
	}: {
		label: string;
		value: string;
		sub?: string;
		ratio?: number | null;
		warn?: number;
		crit?: number;
	} = $props();
</script>

<div class="card flex min-w-0 flex-col gap-1.5">
	<span class="label">{label}</span>
	<span class="tabular truncate text-2xl font-semibold">{value}</span>
	{#if sub}<span class="tabular truncate text-xs text-zinc-500 dark:text-zinc-400">{sub}</span>{/if}
	{#if ratio != null}<Meter value={ratio} {warn} {crit} compact />{/if}
</div>
