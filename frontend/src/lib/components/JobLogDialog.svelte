<script lang="ts">
	// Live output of a dashboard job (e.g. apt dist-upgrade via SSH), streamed via SSE.
	import { onMount, tick } from 'svelte';
	import { getJson, send } from '#lib/api.js';
	import { auth } from '#lib/auth.svelte.js';
	import { dateTime } from '#lib/format.js';
	import type { Job } from '#lib/types.js';
	import Modal from './Modal.svelte';

	let { id, onclose }: { id: number; onclose: () => void } = $props();

	let job = $state<Job | null>(null);
	let lines = $state<string[]>([]);
	let pre: HTMLPreElement | undefined = $state();
	let follow = $state(true);

	const KIND: Record<string, string> = {
		upgrade: 'Paket-Upgrade', 'apt-refresh': 'Paketlisten aktualisieren',
		'maintenance-on': 'Wartungsmodus aktivieren', 'maintenance-off': 'Wartungsmodus beenden',
		reboot: 'Neustart', 'guest-upgrade': 'Paket-Upgrade im Gast'
	};

	async function scroll() {
		await tick();
		if (follow) pre?.scrollTo({ top: pre.scrollHeight });
	}

	onMount(() => {
		getJson<Job>(`/api/v1/jobs/${id}`).then((j) => (job = j));
		const source = new EventSource(`/api/v1/jobs/${id}/stream`);
		source.addEventListener('line', (ev) => {
			lines = [...lines, JSON.parse((ev as MessageEvent).data)];
			scroll();
		});
		source.addEventListener('done', (ev) => {
			job = JSON.parse((ev as MessageEvent).data);
			source.close();
		});
		source.onerror = () => source.close();
		return () => source.close();
	});

	const color = $derived(
		job?.status === 'running' ? 'bg-blue-500/15 text-blue-500'
			: job?.status === 'ok' ? 'bg-emerald-500/15 text-emerald-500' : 'bg-red-500/15 text-red-500'
	);
</script>

<Modal title="{KIND[job?.kind ?? ''] ?? job?.kind ?? 'Job'} · {job?.node ?? ''}" subtitle="Job #{id}{job ? ` · ${job.started_by} · ${dateTime(Date.parse(job.started_at) / 1000)}` : ''}" {onclose} width="900px">
	<div class="mb-3 flex flex-wrap items-center gap-3 text-xs">
		{#if job}
			<span class="rounded-full px-2 py-0.5 font-medium {color}">{job.status === 'running' ? 'läuft …' : job.status}</span>
			{#if job.summary}<span>{job.summary}</span>{/if}
		{/if}
		<label class="ml-auto flex items-center gap-1 text-zinc-500"><input type="checkbox" bind:checked={follow} /> mitlaufen</label>
		{#if job?.status === 'running' && auth.can('admin')}
			<button class="text-red-500 hover:underline" onclick={() => send('POST', `/api/v1/jobs/${id}/cancel`)}>Abbrechen</button>
		{/if}
	</div>
	<pre bind:this={pre} class="h-[55vh] overflow-auto rounded-lg bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-200">{#each lines as l, i (i)}<span class:text-emerald-400={l.includes('✓') || l.includes('== OK')} class:text-red-400={l.includes('✗') || l.includes('== ERROR')} class:text-zinc-500={l.startsWith('[') && l.includes('Get:')}>{l}</span>{'\n'}{:else}Warte auf Ausgabe …{/each}</pre>
</Modal>
