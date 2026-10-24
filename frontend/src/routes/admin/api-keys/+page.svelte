<script lang="ts">
	import { onMount } from 'svelte';
	import { getJson, send } from '#lib/api.js';
	import { dateTime } from '#lib/format.js';
	import { toasts } from '#lib/toast.svelte.js';
	import ConfirmDialog from '#lib/components/ConfirmDialog.svelte';
	import Modal from '#lib/components/Modal.svelte';

	interface Key {
		id: number;
		name: string;
		prefix: string;
		scopes: string[];
		created_by: string;
		created_at: string;
		expires_at: string | null;
		last_used_at: string | null;
		last_used_ip: string | null;
		revoked: boolean;
	}

	let keys = $state<Key[]>([]);
	let name = $state('');
	let scope = $state<'read' | 'control' | 'admin'>('read');
	let days = $state<number | null>(365);
	let created = $state<{ name: string; secret: string } | null>(null);
	let revoke = $state<Key | null>(null);

	const load = async () => (keys = await getJson<Key[]>('/api/v1/admin/api-keys'));
	onMount(load);

	async function create(ev: Event) {
		ev.preventDefault();
		try {
			const r = await send<{ key: Key; secret: string }>('POST', '/api/v1/admin/api-keys', {
				name, scopes: [scope], expires_days: days || null
			});
			created = { name: r.key.name, secret: r.secret };
			name = '';
			load();
		} catch (e) {
			toasts.push('error', (e as Error).message);
		}
	}

	async function copy(text: string) {
		try {
			await navigator.clipboard.writeText(text);
			toasts.push('ok', 'In die Zwischenablage kopiert');
		} catch {
			toasts.push('error', 'Kopieren nicht möglich – bitte manuell markieren');
		}
	}

	const origin = typeof location !== 'undefined' ? location.origin : '';
</script>

<svelte:head><title>API-Keys · Proxmox Dashboard</title></svelte:head>

<h1 class="mb-4 text-xl font-semibold">API-Keys</h1>

<form onsubmit={create} class="card mb-4 flex flex-wrap items-end gap-2 text-sm">
	<label class="flex flex-col gap-1"><span class="label">Name</span><input class="input" bind:value={name} placeholder="z. B. home-assistant" required pattern="[A-Za-z0-9 ._\-]+" /></label>
	<label class="flex flex-col gap-1"><span class="label">Scope</span>
		<select class="input" bind:value={scope}>
			<option value="read">read – nur lesen (Monitoring, Prometheus, HA)</option>
			<option value="control">control – zusätzlich Gäste steuern (z. B. MCP)</option>
			<option value="admin">admin – zusätzlich Node-Aktionen, Alarm-Regeln</option>
		</select>
	</label>
	<label class="flex flex-col gap-1"><span class="label">Gültig (Tage)</span><input class="input w-28" type="number" min="1" max="3650" bind:value={days} placeholder="unbegrenzt" /></label>
	<button class="rounded-lg bg-accent px-3 py-1.5 font-medium text-white">Erstellen</button>
</form>

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[860px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr><th class="px-4 py-3">Name</th><th class="px-4 py-3">Key</th><th class="px-4 py-3">Scope</th><th class="px-4 py-3">Erstellt</th><th class="px-4 py-3">Läuft ab</th><th class="px-4 py-3">Zuletzt benutzt</th><th></th></tr>
		</thead>
		<tbody>
			{#each keys as k (k.id)}
				<tr class="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60" class:opacity-50={k.revoked}>
					<td class="px-4 py-2 font-medium">{k.name}</td>
					<td class="px-4 py-2 font-mono text-xs">{k.prefix}…</td>
					<td class="px-4 py-2">{k.scopes.join(', ')}</td>
					<td class="px-4 py-2 text-xs">{dateTime(Date.parse(k.created_at) / 1000)}<div class="text-zinc-500">{k.created_by}</div></td>
					<td class="px-4 py-2 text-xs">{k.expires_at ? dateTime(Date.parse(k.expires_at) / 1000) : 'nie'}</td>
					<td class="px-4 py-2 text-xs">{k.last_used_at ? dateTime(Date.parse(k.last_used_at) / 1000) : 'nie'}<div class="text-zinc-500">{k.last_used_ip ?? ''}</div></td>
					<td class="px-4 py-2 text-right text-xs">{#if k.revoked}widerrufen{:else}<button class="text-red-500 hover:underline" onclick={() => (revoke = k)}>Widerrufen</button>{/if}</td>
				</tr>
			{:else}<tr><td colspan="7" class="px-4 py-6 text-center text-zinc-500">Noch keine API-Keys.</td></tr>{/each}
		</tbody>
	</table>
</div>

<div class="card mt-4 text-sm">
	<span class="label">Verwendung</span>
	<pre class="mt-2 overflow-x-auto rounded-lg bg-zinc-100 p-3 font-mono text-xs dark:bg-zinc-950">curl -H "Authorization: Bearer pvd_…" {origin}/api/v1/clusters

# Prometheus (scrape_configs)
- job_name: proxmox-dashboard
  metrics_path: /metrics
  authorization: {'{'} credentials: pvd_… {'}'}
  static_configs: [{'{'} targets: ['{origin.replace(/^https?:\/\//, '')}'] {'}'}]</pre>
	<p class="mt-2 text-xs text-zinc-500">API-Doku: <a class="text-accent hover:underline" href="/api/docs" target="_blank">/api/docs</a>. Keys brauchen kein CSRF-Token und können keine Benutzer oder andere Keys verwalten.</p>
</div>

{#if created}
	<Modal title="API-Key „{created.name}“" onclose={() => (created = null)}>
		<p class="mb-3">Der Key wird <strong>nur jetzt</strong> angezeigt. Speichere ihn sicher (z. B. im Passwort-Manager).</p>
		<code class="block rounded-lg bg-zinc-100 px-3 py-2 font-mono text-xs break-all select-all dark:bg-zinc-800">{created.secret}</code>
		<button class="mt-3 text-sm text-accent hover:underline" onclick={() => copy(created!.secret)}>Kopieren</button>
	</Modal>
{/if}

{#if revoke}
	{@const k = revoke}
	<ConfirmDialog title="API-Key „{k.name}“ widerrufen?" message="Tools, die diesen Key nutzen, verlieren sofort den Zugriff." danger confirmLabel="Widerrufen"
		onclose={() => (revoke = null)} onconfirm={async () => { await send('DELETE', `/api/v1/admin/api-keys/${k.id}`); load(); }} />
{/if}
