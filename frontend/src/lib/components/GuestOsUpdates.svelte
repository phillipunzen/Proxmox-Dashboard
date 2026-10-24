<script lang="ts">
	// Optional package updates inside a guest (GUEST_UPDATES=true, Debian/Ubuntu only).
	import { getJson, send } from '#lib/api.js';
	import { auth } from '#lib/auth.svelte.js';
	import { jobViewer, toasts } from '#lib/toast.svelte.js';
	import type { GuestView } from '#lib/types.js';
	import ConfirmDialog from './ConfirmDialog.svelte';

	let { guest }: { guest: GuestView } = $props();

	let result = $state<{ os: string; updates: number } | null>(null);
	let checking = $state(false);
	let confirm = $state(false);
	const base = $derived(`/api/v1/clusters/${encodeURIComponent(guest.cluster)}/guests/${guest.vmid}`);

	async function check() {
		checking = true;
		try {
			result = await getJson(`${base}/os-updates`);
		} catch (e) {
			toasts.push('error', (e as Error).message);
		} finally {
			checking = false;
		}
	}

	async function upgrade(typed: string) {
		const r = await send<{ job_id: number }>('POST', `${base}/os-upgrade`, { confirm: typed });
		jobViewer.open(r.job_id);
		result = null;
	}
</script>

{#if auth.canControl(guest.cluster) && guest.status === 'running'}
	<div class="card flex flex-wrap items-center gap-3 text-sm">
		<span class="label mr-auto">Pakete im Gast</span>
		{#if result}
			<span>{result.os}: <strong>{result.updates}</strong> Update(s)</span>
		{/if}
		<button class="rounded-lg border border-zinc-300 px-3 py-1.5 hover:border-accent hover:text-accent disabled:opacity-40 dark:border-zinc-700" disabled={checking} onclick={check}>
			{checking ? 'Prüfe …' : 'Updates prüfen'}
		</button>
		{#if result?.updates}
			<button class="rounded-lg border border-zinc-300 px-3 py-1.5 hover:border-accent hover:text-accent dark:border-zinc-700" onclick={() => (confirm = true)}>Installieren</button>
		{/if}
	</div>
{/if}

{#if confirm}
	<ConfirmDialog title="Pakete in {guest.name} aktualisieren" danger requireText={guest.name} confirmLabel="Installieren"
		message="Führt im Gast 'apt-get update && apt-get dist-upgrade' aus ({guest.type === 'qemu' ? 'über den QEMU Guest Agent' : 'per pct exec auf dem Node'}). Dienste im Gast können dabei neu starten."
		onconfirm={upgrade} onclose={() => (confirm = false)} />
{/if}
