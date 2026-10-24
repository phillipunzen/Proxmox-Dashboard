<script lang="ts">
	import { toasts } from '#lib/toast.svelte.js';
</script>

<div class="pointer-events-none fixed right-4 bottom-4 z-50 flex w-[min(380px,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
	{#each toasts.items as t (t.id)}
		<div class="pointer-events-auto flex items-start gap-3 rounded-xl border px-4 py-3 text-sm shadow-lg {t.kind === 'error'
				? 'border-red-500/40 bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300'
				: 'border-zinc-200 bg-white dark:border-zinc-700 dark:bg-zinc-900'}">
			<span class="mt-0.5">{t.kind === 'ok' ? '✓' : t.kind === 'error' ? '⚠' : 'ℹ'}</span>
			<span class="flex-1">{t.text}</span>
			{#if t.action}
				<button class="font-medium text-accent hover:underline" onclick={() => { t.action?.run(); toasts.dismiss(t.id); }}>{t.action.label}</button>
			{/if}
			<button class="text-zinc-400 hover:text-zinc-600" onclick={() => toasts.dismiss(t.id)} aria-label="Schließen">✕</button>
		</div>
	{/each}
</div>
