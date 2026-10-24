<script lang="ts">
	// Modal with live SMART details of one disk.
	import { onMount } from 'svelte';
	import { getJson } from '#lib/api.js';
	import type { Disk } from '#lib/types.js';

	interface SmartAttr {
		id: string;
		name: string;
		value: number;
		worst: number;
		threshold: number;
		raw: string;
		fail: string;
	}
	interface Smart {
		health: string;
		type: 'ata' | 'text';
		attributes?: SmartAttr[];
		text?: string;
	}

	let { cluster, node, disk, onclose }: { cluster: string; node: string; disk: Disk; onclose: () => void } = $props();

	let dialog: HTMLDialogElement;
	let data = $state<Smart | null>(null);
	let error = $state<string | null>(null);

	onMount(() => {
		dialog.showModal();
		getJson<Smart>(`/api/v1/inventory/clusters/${encodeURIComponent(cluster)}/nodes/${encodeURIComponent(node)}/smart`, { disk: disk.devpath })
			.then((d) => (data = d))
			.catch((e) => (error = e.message));
	});
</script>

<dialog
	bind:this={dialog}
	onclose={onclose}
	class="m-auto w-[min(720px,calc(100vw-2rem))] rounded-xl border border-zinc-200 bg-white p-0 text-zinc-800 backdrop:bg-black/50 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-200"
>
	<div class="flex items-center justify-between border-b border-zinc-200 px-5 py-3 dark:border-zinc-800">
		<div>
			<div class="font-medium">SMART · {node} · {disk.devpath}</div>
			<div class="text-xs text-zinc-500">{disk.model} · {disk.serial}</div>
		</div>
		<button class="rounded-lg px-2 py-1 hover:bg-zinc-100 dark:hover:bg-zinc-800" onclick={() => dialog.close()} aria-label="Schließen">✕</button>
	</div>
	<div class="max-h-[70vh] overflow-auto p-5 text-sm">
		{#if error}
			<p class="text-red-500">{error}</p>
		{:else if !data}
			<p class="text-zinc-500">Lade …</p>
		{:else}
			<p class="mb-3">Gesamtstatus: <strong class={data.health === 'FAILED' ? 'text-red-500' : 'text-emerald-500'}>{data.health}</strong></p>
			{#if data.attributes}
				<table class="w-full text-xs">
					<thead class="text-left text-zinc-500">
						<tr><th class="py-1">ID</th><th>Attribut</th><th class="text-right">Wert</th><th class="text-right">Worst</th><th class="text-right">Schwelle</th><th class="text-right">Raw</th><th>Status</th></tr>
					</thead>
					<tbody>
						{#each data.attributes as a (a.id)}
							<tr class="border-t border-zinc-100 dark:border-zinc-800" class:text-red-500={a.fail !== '-'}>
								<td class="py-1">{a.id.trim()}</td><td>{a.name}</td>
								<td class="tabular text-right">{a.value}</td><td class="tabular text-right">{a.worst}</td>
								<td class="tabular text-right">{a.threshold}</td><td class="tabular text-right">{a.raw}</td>
								<td class="pl-2">{a.fail}</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{:else}
				<pre class="font-mono text-xs whitespace-pre-wrap">{data.text}</pre>
			{/if}
		{/if}
	</div>
</dialog>
