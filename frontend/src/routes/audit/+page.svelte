<script lang="ts">
	import { getJson } from '#lib/api.js';
	import { auth } from '#lib/auth.svelte.js';
	import { dateTime } from '#lib/format.js';
	import { taskViewer } from '#lib/toast.svelte.js';

	interface Entry {
		id: number;
		ts: string;
		actor: string;
		actor_role: string | null;
		ip: string | null;
		action: string;
		cluster: string | null;
		node: string | null;
		vmid: number | null;
		target: string | null;
		params: Record<string, unknown>;
		dry_run: boolean;
		status: string;
		upid: string | null;
		message: string | null;
	}

	let entries = $state<Entry[]>([]);
	let action = $state('');
	let status = $state('');
	let actor = $state('');
	let more = $state(true);

	async function load(append = false) {
		const before = append ? entries.at(-1)?.id : undefined;
		const rows = await getJson<Entry[]>('/api/v1/audit', { action, status, actor_name: actor, before_id: before, limit: 100 });
		entries = append ? [...entries, ...rows] : rows;
		more = rows.length === 100;
	}

	$effect(() => {
		void [action, status, actor];
		load();
		const timer = setInterval(() => load(), 10_000);
		return () => clearInterval(timer);
	});

	const colors: Record<string, string> = {
		ok: 'text-emerald-500', error: 'text-red-500', denied: 'text-red-500',
		submitted: 'text-blue-500', running: 'text-blue-500', unknown: 'text-amber-500'
	};
</script>

<svelte:head><title>Audit-Log · Proxmox Dashboard</title></svelte:head>

<div class="mb-4 flex flex-wrap items-center gap-2">
	<h1 class="mr-auto text-xl font-semibold">Audit-Log</h1>
	{#if !auth.can('admin')}<span class="text-xs text-zinc-500">Du siehst nur deine eigenen Einträge.</span>{/if}
	<select class="input" bind:value={action} aria-label="Aktion">
		<option value="">Alle Aktionen</option>
		<option value="guest.">Gast-Aktionen</option>
		<option value="auth.">Anmeldungen</option>
		<option value="user.">Benutzerverwaltung</option>
	</select>
	<select class="input" bind:value={status} aria-label="Ergebnis">
		<option value="">Jedes Ergebnis</option>
		<option value="ok">ok</option><option value="error">Fehler</option><option value="denied">abgelehnt</option><option value="submitted">läuft</option>
	</select>
	{#if auth.can('admin')}<input class="input w-36" placeholder="Benutzer" bind:value={actor} aria-label="Benutzer" />{/if}
</div>

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[1000px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr><th class="px-4 py-3">Zeit</th><th class="px-4 py-3">Wer</th><th class="px-4 py-3">Aktion</th><th class="px-4 py-3">Ziel</th><th class="px-4 py-3">Ergebnis</th><th class="px-4 py-3">Details</th></tr>
		</thead>
		<tbody>
			{#each entries as e (e.id)}
				<tr class="border-b border-zinc-100 align-top last:border-0 dark:border-zinc-800/60">
					<td class="px-4 py-2 text-xs whitespace-nowrap">{dateTime(Date.parse(e.ts) / 1000)}</td>
					<td class="px-4 py-2">{e.actor}<div class="text-xs text-zinc-500">{[e.actor_role, e.ip].filter(Boolean).join(' · ')}</div></td>
					<td class="px-4 py-2 font-mono text-xs">{e.action}{#if e.dry_run}<span class="ml-1 rounded bg-violet-500/15 px-1 text-violet-500">dry-run</span>{/if}</td>
					<td class="px-4 py-2">
						{#if e.vmid}<a class="hover:text-accent" href="/guests/{e.cluster}/{e.vmid}">{e.vmid} {e.target ?? ''}</a>{:else}{e.target ?? ''}{/if}
						{#if e.node}<div class="text-xs text-zinc-500">{e.cluster} / {e.node}</div>{/if}
					</td>
					<td class="px-4 py-2 font-medium {colors[e.status] ?? ''}">{e.status}</td>
					<td class="px-4 py-2 text-xs">
						{#if Object.keys(e.params).length}<div class="font-mono text-zinc-500">{JSON.stringify(e.params)}</div>{/if}
						{#if e.message}<div>{e.message}</div>{/if}
						{#if e.upid && e.cluster}<button class="text-accent hover:underline" onclick={() => taskViewer.open(e.cluster!, e.upid!)}>Task-Log</button>{/if}
					</td>
				</tr>
			{:else}
				<tr><td colspan="6" class="px-4 py-8 text-center text-zinc-500">Keine Einträge.</td></tr>
			{/each}
		</tbody>
	</table>
</div>
{#if more}<button class="mt-3 text-sm text-accent hover:underline" onclick={() => load(true)}>Ältere laden</button>{/if}
