<script lang="ts">
	// Confirmation for critical actions. Destructive ones require typing a word (guest name).
	import type { Snippet } from 'svelte';
	import Modal from './Modal.svelte';

	let {
		title,
		message,
		confirmLabel = 'Ausführen',
		danger = false,
		requireText = '',
		onconfirm,
		onclose,
		children
	}: {
		title: string;
		message: string;
		confirmLabel?: string;
		danger?: boolean;
		/** If set, the user must type exactly this text to enable the button. */
		requireText?: string;
		onconfirm: (typed: string) => Promise<void> | void;
		onclose: () => void;
		children?: Snippet;
	} = $props();

	let typed = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let modal: Modal;

	const enabled = $derived(!busy && (!requireText || typed === requireText));

	async function run(ev: Event) {
		ev.preventDefault();
		if (!enabled) return;
		busy = true;
		error = null;
		try {
			await onconfirm(typed);
			modal.close();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}
</script>

<Modal bind:this={modal} {title} {onclose}>
	<form id="confirm-form" onsubmit={run} class="flex flex-col gap-3">
		<p>{message}</p>
		{@render children?.()}
		{#if requireText}
			<label class="flex flex-col gap-1">
				<span class="text-xs text-zinc-500">Zur Bestätigung <strong class="font-mono text-zinc-800 dark:text-zinc-200">{requireText}</strong> eingeben</span>
				<!-- svelte-ignore a11y_autofocus -->
				<input class="input font-mono" bind:value={typed} autocomplete="off" spellcheck="false" autofocus />
			</label>
		{/if}
		{#if error}<p class="rounded-lg bg-red-500/10 px-3 py-2 text-red-600 dark:text-red-400">{error}</p>{/if}
	</form>
	{#snippet footer()}
		<button class="input" type="button" onclick={() => modal.close()}>Abbrechen</button>
		<button
			form="confirm-form"
			type="submit"
			disabled={!enabled}
			class="rounded-lg px-3 py-1.5 text-sm font-medium text-white disabled:opacity-40 {danger ? 'bg-red-600 hover:bg-red-500' : 'bg-accent hover:brightness-110'}"
		>
			{busy ? 'Bitte warten …' : confirmLabel}
		</button>
	{/snippet}
</Modal>
