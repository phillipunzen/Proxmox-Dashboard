<script lang="ts">
	import type { RangeKey } from '#lib/types.js';

	let { value = $bindable('24h') }: { value?: RangeKey } = $props();
	const ranges: RangeKey[] = ['1h', '6h', '24h', '7d', '30d'];
</script>

<div class="inline-flex rounded-lg border border-zinc-300 p-0.5 text-xs dark:border-zinc-700" role="group" aria-label="Zeitraum">
	{#each ranges as r (r)}
		<button
			class="rounded-md px-2.5 py-1 font-medium transition-colors {value === r
				? 'bg-accent text-white'
				: 'text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200'}"
			aria-pressed={value === r}
			onclick={() => (value = r)}>{r}</button
		>
	{/each}
</div>
