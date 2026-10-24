<script lang="ts">
	// Accessible modal on top of the native <dialog> element (focus trap + Esc for free).
	import { onMount, type Snippet } from 'svelte';

	let {
		title,
		subtitle = '',
		onclose,
		children,
		footer,
		width = '560px'
	}: {
		title: string;
		subtitle?: string;
		onclose: () => void;
		children: Snippet;
		footer?: Snippet;
		width?: string;
	} = $props();

	let dialog: HTMLDialogElement;
	onMount(() => dialog.showModal());

	export function close() {
		dialog.close();
	}
</script>

<dialog
	bind:this={dialog}
	{onclose}
	style:width="min({width}, calc(100vw - 2rem))"
	class="m-auto rounded-xl border border-zinc-200 bg-white p-0 text-zinc-800 backdrop:bg-black/50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-200"
>
	<div class="flex items-start justify-between gap-4 border-b border-zinc-200 px-5 py-3 dark:border-zinc-800">
		<div>
			<div class="font-medium">{title}</div>
			{#if subtitle}<div class="text-xs text-zinc-500">{subtitle}</div>{/if}
		</div>
		<button class="rounded-lg px-2 py-1 hover:bg-zinc-100 dark:hover:bg-zinc-800" onclick={() => dialog.close()} aria-label="Schließen">✕</button>
	</div>
	<div class="max-h-[70vh] overflow-auto p-5 text-sm">{@render children()}</div>
	{#if footer}
		<div class="flex justify-end gap-2 border-t border-zinc-200 px-5 py-3 dark:border-zinc-800">{@render footer()}</div>
	{/if}
</dialog>
