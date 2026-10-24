<script lang="ts">
	import { onMount } from 'svelte';
	import { getJson, send } from '#lib/api.js';
	import { toasts } from '#lib/toast.svelte.js';
	import Modal from '#lib/components/Modal.svelte';

	interface SshNode {
		cluster: string;
		node: string;
		host: string | null;
		trusted: boolean;
		host_key: string | null;
	}
	interface Overview {
		available: boolean;
		user: string;
		port: number;
		public_key: string | null;
		nodes: SshNode[];
	}

	let data = $state<Overview | null>(null);
	let scan = $state<{ n: SshNode; key: string; fingerprint: string; host: string } | null>(null);
	let busy = $state('');

	const load = async () => (data = await getJson<Overview>('/api/v1/admin/ssh'));
	onMount(load);

	async function doScan(n: SshNode) {
		busy = n.node;
		try {
			const r = await send<{ key: string; fingerprint: string; host: string }>('POST', '/api/v1/admin/ssh/scan', { cluster: n.cluster, node: n.node });
			scan = { n, ...r };
		} catch (e) {
			toasts.push('error', (e as Error).message);
		} finally {
			busy = '';
		}
	}

	async function trust(n: SshNode, key: string | null) {
		await send('POST', '/api/v1/admin/ssh/trust', { cluster: n.cluster, node: n.node, key });
		scan = null;
		await load();
	}

	async function test(n: SshNode) {
		busy = n.node;
		const r = await send<{ ok: boolean; message: string; mainboard?: string }>('POST', '/api/v1/admin/ssh/test', { cluster: n.cluster, node: n.node });
		busy = '';
		toasts.push(r.ok ? 'ok' : 'error', r.ok ? `${n.node}: ${r.message}${r.mainboard ? ` · ${r.mainboard}` : ''}` : `${n.node}: ${r.message}`);
	}

	const setupCmd = $derived(data?.public_key ? `bash node-setup.sh "${data.public_key}"` : '');
</script>

<svelte:head><title>SSH · Proxmox Dashboard</title></svelte:head>

<h1 class="mb-4 text-xl font-semibold">SSH-Zugang zu den Nodes</h1>

<div class="card mb-4 text-sm">
	<p>SSH wird nur für das gebraucht, was die Proxmox-API nicht kann: <strong>Paket-Upgrades</strong> (mit Live-Ausgabe), <strong>Mainboard-Infos</strong> und <strong>Temperaturen</strong>. Der Schlüssel darf auf dem Node ausschließlich ein festes Hilfsskript starten – keine Shell.</p>
	<ol class="mt-3 list-decimal space-y-2 pl-5">
		<li><code>deploy/node-setup.sh</code> auf jeden Node kopieren und als root ausführen:
			<pre class="mt-1 overflow-x-auto rounded-lg bg-zinc-100 p-2 font-mono text-xs dark:bg-zinc-950">{setupCmd || 'SSH-Schlüssel nicht verfügbar'}</pre>
		</li>
		<li>Unten den Host-Key jedes Nodes abrufen und mit dem Fingerprint vergleichen, den das Skript am Ende ausgibt – dann bestätigen.</li>
		<li>„Testen“ prüft Anmeldung und Hilfsskript.</li>
	</ol>
	<p class="mt-3 text-xs text-zinc-500">Benutzer <code>{data?.user}</code>, Port {data?.port}. Abweichende Adressen: <code>PVE_n_SSH_HOSTS=pve1=10.0.0.11,…</code></p>
</div>

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[760px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr><th class="px-4 py-3">Node</th><th class="px-4 py-3">Adresse</th><th class="px-4 py-3">Host-Key</th><th></th></tr>
		</thead>
		<tbody>
			{#each data?.nodes ?? [] as n (n.cluster + n.node)}
				<tr class="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60">
					<td class="px-4 py-2 font-medium">{n.node}<div class="text-xs text-zinc-500">{n.cluster}</div></td>
					<td class="px-4 py-2 font-mono text-xs">{n.host ?? '–'}</td>
					<td class="px-4 py-2 text-xs">
						{#if n.trusted}<span class="text-emerald-500">✓ bestätigt</span><div class="max-w-md truncate font-mono text-zinc-500">{n.host_key}</div>
						{:else}<span class="text-amber-500">nicht bestätigt</span>{/if}
					</td>
					<td class="px-4 py-2 text-right text-xs whitespace-nowrap">
						{#if busy === n.node}…{:else}
							<button class="text-accent hover:underline" onclick={() => doScan(n)}>{n.trusted ? 'Neu abrufen' : 'Host-Key abrufen'}</button>
							{#if n.trusted} · <button class="hover:text-accent" onclick={() => test(n)}>Testen</button> · <button class="text-red-500 hover:underline" onclick={() => trust(n, null)}>Vergessen</button>{/if}
						{/if}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>

{#if scan}
	{@const s = scan}
	<Modal title="Host-Key von {s.n.node} ({s.host})" onclose={() => (scan = null)}>
		<p class="mb-2">Vergleiche den Fingerprint mit der Ausgabe von <code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code> auf dem Node:</p>
		<code class="block rounded-lg bg-zinc-100 px-3 py-2 font-mono text-sm break-all dark:bg-zinc-800">{s.fingerprint}</code>
		{#if s.n.host_key && s.n.host_key !== s.key}
			<p class="mt-3 rounded-lg bg-red-500/10 px-3 py-2 text-red-500">⚠ Der Schlüssel hat sich gegenüber dem bestätigten geändert! Nur übernehmen, wenn der Node neu installiert wurde.</p>
		{/if}
		{#snippet footer()}
			<button class="input" onclick={() => (scan = null)}>Abbrechen</button>
			<button class="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white" onclick={() => trust(s.n, s.key)}>Fingerprint stimmt – vertrauen</button>
		{/snippet}
	</Modal>
{/if}
