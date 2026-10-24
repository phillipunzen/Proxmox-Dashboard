<script lang="ts">
	// Live log of a Proxmox task; polls while the task is running.
	import { onMount, tick } from 'svelte';
	import { getJson } from '#lib/api.js';
	import { dateTime } from '#lib/format.js';
	import Modal from './Modal.svelte';

	let { cluster, upid, onclose }: { cluster: string; upid: string; onclose: () => void } = $props();

	interface Status {
		status: 'running' | 'stopped';
		exitstatus?: string;
		type: string;
		id?: string;
		node: string;
		user: string;
		starttime: number;
	}

	let status = $state<Status | null>(null);
	let lines = $state<{ n: number; t: string }[]>([]);
	let error = $state<string | null>(null);
	let pre: HTMLPreElement | undefined = $state();

	const base = $derived(`/api/v1/tasks/${encodeURIComponent(cluster)}/${encodeURIComponent(upid)}`);

	async function poll() {
		try {
			status = await getJson<Status>(`${base}/status`);
			const more = await getJson<{ n: number; t: string }[]>(`${base}/log`, { start: lines.length });
			if (more.length) {
				lines = [...lines, ...more];
				await tick();
				pre?.scrollTo({ top: pre.scrollHeight });
			}
			error = null;
		} catch (e) {
			error = (e as Error).message;
		}
	}

	onMount(() => {
		poll();
		const timer = setInterval(() => status?.status !== 'stopped' && poll(), 2000);
		return () => clearInterval(timer);
	});

	const ok = $derived(status?.exitstatus === 'OK' || status?.exitstatus?.startsWith('WARNINGS'));
</script>

<Modal title="Task {status?.type ?? ''} {status?.id ?? ''}" subtitle={upid} {onclose} width="760px">
	<div class="mb-3 flex flex-wrap items-center gap-3 text-xs">
		{#if status}
			<span class="rounded-full px-2 py-0.5 font-medium {status.status === 'running' ? 'bg-blue-500/15 text-blue-500' : ok ? 'bg-emerald-500/15 text-emerald-500' : 'bg-red-500/15 text-red-500'}">
				{status.status === 'running' ? 'läuft …' : status.exitstatus}
			</span>
			<span class="text-zinc-500">{status.node} · {status.user} · gestartet {dateTime(status.starttime)}</span>
		{/if}
	</div>
	{#if error}<p class="mb-2 text-red-500">{error}</p>{/if}
	<pre bind:this={pre} class="max-h-[50vh] overflow-auto rounded-lg bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-200">{#each lines as l (l.n)}{l.t}{'\n'}{:else}…{/each}</pre>
</Modal>
