<script lang="ts">
	// TOTP enrolment: QR code + manual secret + confirmation code.
	import { onMount } from 'svelte';
	import { send } from '#lib/api.js';
	import type { Me } from '#lib/auth.svelte.js';

	let { ondone }: { ondone: (me: Me) => void } = $props();

	let setup = $state<{ secret: string; uri: string; qr_svg: string } | null>(null);
	let code = $state('');
	let error = $state<string | null>(null);
	let busy = $state(false);

	onMount(async () => {
		try {
			setup = await send('POST', '/api/v1/auth/totp/setup');
		} catch (e) {
			error = (e as Error).message;
		}
	});

	async function confirm(ev: Event) {
		ev.preventDefault();
		busy = true;
		error = null;
		try {
			ondone(await send<Me>('POST', '/api/v1/auth/totp/enable', { code }));
		} catch (e) {
			error = (e as Error).message;
			code = '';
		} finally {
			busy = false;
		}
	}
</script>

<div class="flex flex-col gap-4 text-sm">
	<p>Scanne den Code mit einer Authenticator-App (z. B. Aegis, 2FAS, Google Authenticator, 1Password) und gib den angezeigten 6-stelligen Code ein.</p>
	{#if setup}
		<!-- QR code is generated server-side as SVG from our own data -->
		<div class="mx-auto w-48 rounded-lg bg-white p-2 [&_svg]:h-auto [&_svg]:w-full">{@html setup.qr_svg}</div>
		<details class="text-xs text-zinc-500">
			<summary class="cursor-pointer">Code manuell eingeben</summary>
			<code class="mt-1 block font-mono tracking-wider break-all text-zinc-800 select-all dark:text-zinc-200">{setup.secret}</code>
		</details>
	{/if}
	<form onsubmit={confirm} class="flex gap-2">
		<input class="input flex-1 text-center font-mono text-lg tracking-[0.4em]" bind:value={code} inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="000000" aria-label="Bestätigungscode" />
		<button class="rounded-lg bg-accent px-4 text-sm font-medium text-white disabled:opacity-40" disabled={busy || code.length !== 6}>Aktivieren</button>
	</form>
	{#if error}<p class="text-red-500">{error}</p>{/if}
</div>
