<script lang="ts">
	// Horizontal utilisation bar with label and value text.
	import { levelColor } from '#lib/format.js';

	let {
		value,
		label = '',
		text = '',
		warn = 0.75,
		crit = 0.9,
		compact = false
	}: {
		value: number;
		label?: string;
		text?: string;
		warn?: number;
		crit?: number;
		compact?: boolean;
	} = $props();

	const clamped = $derived(Math.max(0, Math.min(1, value || 0)));
</script>

<div class="min-w-0" title={label ? `${label}: ${text}` : text}>
	{#if !compact}
		<div class="mb-1 flex items-baseline justify-between gap-2 text-xs">
			<span class="text-zinc-500 dark:text-zinc-400">{label}</span>
			<span class="tabular truncate">{text}</span>
		</div>
	{/if}
	<div
		class="h-1.5 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-800"
		role="meter"
		aria-label={label}
		aria-valuemin={0}
		aria-valuemax={100}
		aria-valuenow={Math.round(clamped * 100)}
	>
		<div
			class="h-full rounded-full transition-[width] duration-500 {levelColor(clamped, warn, crit)}"
			style:width="{clamped * 100}%"
		></div>
	</div>
</div>
