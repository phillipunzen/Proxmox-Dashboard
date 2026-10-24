<script lang="ts">
	import { onMount } from 'svelte';
	import { getJson, postJson, url } from '#lib/api.js';
	import { live } from '#lib/live.svelte.js';
	import { bytes, timeAgo } from '#lib/format.js';
	import StatusBadge from '#lib/components/StatusBadge.svelte';
	import SmartDialog from '#lib/components/SmartDialog.svelte';
	import type { ClusterInventory, Disk } from '#lib/types.js';

	type Tab = 'nodes' | 'disks' | 'network' | 'pci' | 'guests';
	const tabs: { key: Tab; label: string; export: string }[] = [
		{ key: 'nodes', label: 'Nodes', export: 'nodes' },
		{ key: 'disks', label: 'Datenträger', export: 'disks' },
		{ key: 'network', label: 'Netzwerk', export: 'network' },
		{ key: 'pci', label: 'PCI / Passthrough', export: 'pci' },
		{ key: 'guests', label: 'Gäste', export: 'guests' }
	];

	let tab = $state<Tab>('nodes');
	let data = $state<ClusterInventory[]>([]);
	let error = $state<string | null>(null);
	let refreshing = $state(false);
	let smart = $state<{ cluster: string; node: string; disk: Disk } | null>(null);

	async function load() {
		try {
			data = await getJson<ClusterInventory[]>('/api/v1/inventory');
			error = null;
		} catch (e) {
			error = (e as Error).message;
		}
	}

	async function refresh() {
		refreshing = true;
		try {
			await postJson('/api/v1/inventory/refresh');
			await load();
		} finally {
			refreshing = false;
		}
	}

	onMount(() => {
		load();
		// The first inventory run starts with the backend; poll until it is there.
		const timer = setInterval(() => !data.length && load(), 3000);
		return () => clearInterval(timer);
	});

	const shown = $derived(live.selected ? data.filter((c) => c.cluster === live.selected) : data);
	const nodes = $derived(shown.flatMap((c) => c.nodes));
	const guests = $derived(shown.flatMap((c) => c.guests).filter((g) => !g.template));
	const multi = $derived(shown.length > 1);
	const exportKind = $derived(tabs.find((t) => t.key === tab)!.export);
	const exportParams = $derived({ kind: exportKind, cluster: live.selected ?? undefined });

	const healthStatus = (h: string | null) =>
		h === 'PASSED' || h === 'OK' ? 'ok' : h === 'FAILED' ? 'critical' : 'unknown';
	const wearColor = (w: number | null) =>
		w == null ? '' : w >= 80 ? 'text-red-500' : w >= 50 ? 'text-amber-500' : '';
</script>

<svelte:head><title>Inventar · Proxmox Dashboard</title></svelte:head>

<div class="mb-4 flex flex-wrap items-center gap-2">
	<h1 class="mr-auto text-xl font-semibold">Hardware-Inventar</h1>
	<span class="text-xs text-zinc-500">
		Stand: {timeAgo(shown[0]?.collected_at ?? null)}
	</span>
	<button class="input hover:border-accent" onclick={refresh} disabled={refreshing}>
		{refreshing ? 'Aktualisiere …' : 'Jetzt aktualisieren'}
	</button>
	<a class="input hover:border-accent" href={url('/api/v1/inventory/export', { ...exportParams, format: 'csv' })} download>CSV</a>
	<a class="input hover:border-accent" href={url('/api/v1/inventory/export', { ...exportParams, format: 'json' })} download>JSON</a>
</div>

<div class="mb-4 flex flex-wrap gap-1 border-b border-zinc-200 dark:border-zinc-800" role="tablist">
	{#each tabs as t (t.key)}
		<button
			role="tab"
			aria-selected={tab === t.key}
			class="-mb-px border-b-2 px-3 py-2 text-sm font-medium {tab === t.key
				? 'border-accent text-accent'
				: 'border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200'}"
			onclick={() => (tab = t.key)}>{t.label}</button
		>
	{/each}
</div>

{#if error}
	<div class="card text-sm text-red-500">{error}</div>
{:else if !data.length}
	<div class="card text-sm text-zinc-500">Inventar wird erfasst …</div>
{:else if tab === 'nodes'}
	<div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
		{#each nodes as n (n.cluster + n.node)}
			<div class="card text-sm">
				<div class="mb-3 flex items-center justify-between">
					<a href="/nodes/{n.cluster}/{n.node}" class="font-medium hover:text-accent">{n.node}</a>
					<StatusBadge status={n.online ? 'online' : 'offline'} />
				</div>
				<dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
					{#if multi}<dt class="text-zinc-500">Cluster</dt><dd>{n.cluster}</dd>{/if}
					<dt class="text-zinc-500">CPU</dt><dd>{n.cpu_model ?? '–'}</dd>
					<dt class="text-zinc-500">Topologie</dt>
					<dd>{n.cpu_sockets ?? '–'} Socket · {n.cpu_cores ?? '–'} Kerne · {n.cpu_threads ?? '–'} Threads</dd>
					<dt class="text-zinc-500">RAM</dt><dd>{bytes(n.mem_total)}</dd>
					<dt class="text-zinc-500">Mainboard</dt><dd class="text-zinc-500">{n.mainboard ?? 'nicht über die API verfügbar'}</dd>
					<dt class="text-zinc-500">Boot</dt><dd>{n.boot_mode?.toUpperCase() ?? '–'}{n.secure_boot ? ' · Secure Boot' : ''}</dd>
					<dt class="text-zinc-500">PVE / Kernel</dt><dd>{n.pve_version ?? '–'} · {n.kernel ?? '–'}</dd>
					<dt class="text-zinc-500">Datenträger</dt>
					<dd>
						{n.disks.length} ({bytes(n.disks.reduce((s, d) => s + d.size, 0))})
						{#if n.disks.some((d) => healthStatus(d.health) === 'critical')}<span class="ml-1 text-red-500">⚠ SMART</span>{/if}
					</dd>
					<dt class="text-zinc-500">Netzwerk</dt><dd>{n.network.filter((i) => i.type === 'eth').length} NICs · {n.network.filter((i) => i.type !== 'eth').length} virtuell</dd>
					<dt class="text-zinc-500">PCI</dt><dd>{n.pci.length} Geräte · {n.pci.filter((p) => p.assigned_to.length).length} durchgereicht</dd>
				</dl>
				{#if n.errors.length}<p class="mt-2 text-xs text-amber-500" title={n.errors.join('\n')}>{n.errors.length} Abfrage(n) fehlgeschlagen – fehlen Rechte?</p>{/if}
			</div>
		{/each}
	</div>
{:else if tab === 'disks'}
	<div class="card overflow-x-auto p-0">
		<table class="w-full min-w-[900px] text-sm">
			<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
				<tr><th class="px-4 py-3">Node</th><th class="px-4 py-3">Gerät</th><th class="px-4 py-3">Typ</th><th class="px-4 py-3">Modell / Seriennummer</th><th class="px-4 py-3 text-right">Größe</th><th class="px-4 py-3">SMART</th><th class="px-4 py-3 text-right">Verschleiß</th><th class="px-4 py-3">Verwendung</th></tr>
			</thead>
			<tbody>
				{#each nodes as n (n.cluster + n.node)}
					{#each n.disks as d (d.devpath)}
						<tr class="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60">
							<td class="px-4 py-2">{n.node}</td>
							<td class="px-4 py-2 font-mono text-xs">{d.devpath}</td>
							<td class="px-4 py-2 uppercase text-xs">{d.type}{d.rpm ? ` · ${d.rpm} rpm` : ''}</td>
							<td class="px-4 py-2">{d.model ?? '–'}<div class="text-xs text-zinc-500">{d.serial ?? ''}</div></td>
							<td class="tabular px-4 py-2 text-right">{(d.size / 1e9).toLocaleString('de-DE', { maximumFractionDigits: 0 })} GB</td>
							<td class="px-4 py-2">
								<button onclick={() => (smart = { cluster: n.cluster, node: n.node, disk: d })} title="SMART-Details">
									<StatusBadge status={healthStatus(d.health)} label={d.health ?? 'unbekannt'} />
								</button>
							</td>
							<td class="tabular px-4 py-2 text-right {wearColor(d.wearout_used)}">{d.wearout_used != null ? `${d.wearout_used} %` : '–'}</td>
							<td class="px-4 py-2 text-zinc-500">{d.used_by ?? '–'}</td>
						</tr>
					{/each}
				{/each}
			</tbody>
		</table>
	</div>
{:else if tab === 'network'}
	<div class="card overflow-x-auto p-0">
		<table class="w-full min-w-[960px] text-sm">
			<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
				<tr><th class="px-4 py-3">Node</th><th class="px-4 py-3">Interface</th><th class="px-4 py-3">Typ</th><th class="px-4 py-3">Adresse</th><th class="px-4 py-3">Ports / Slaves</th><th class="px-4 py-3">VLAN</th><th class="px-4 py-3 text-right">MTU</th><th class="px-4 py-3">Kommentar</th></tr>
			</thead>
			<tbody>
				{#each nodes as n (n.cluster + n.node)}
					{#each n.network as i (i.iface)}
						<tr class="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60">
							<td class="px-4 py-2">{n.node}</td>
							<td class="px-4 py-2 font-mono text-xs">
								<span class="mr-1 inline-block size-1.5 rounded-full {i.active ? 'bg-emerald-500' : 'bg-zinc-500'}"></span>{i.iface}
							</td>
							<td class="px-4 py-2 text-xs">{i.type}{i.bond_mode ? ` (${i.bond_mode})` : ''}{i.bridge_vlan_aware ? ' · VLAN-aware' : ''}</td>
							<td class="px-4 py-2 font-mono text-xs">{i.cidr ?? '–'}{#if i.gateway}<div class="text-zinc-500">GW {i.gateway}</div>{/if}</td>
							<td class="px-4 py-2 font-mono text-xs">{[...i.bridge_ports, ...i.slaves].join(', ') || '–'}</td>
							<td class="px-4 py-2 text-xs">{i.vlan_id ? `${i.vlan_id} auf ${i.vlan_raw_device}` : '–'}</td>
							<td class="tabular px-4 py-2 text-right">{i.mtu ?? 1500}</td>
							<td class="px-4 py-2 text-zinc-500">{i.comments ?? ''}</td>
						</tr>
					{/each}
				{/each}
			</tbody>
		</table>
	</div>
{:else if tab === 'pci'}
	<div class="card overflow-x-auto p-0">
		<table class="w-full min-w-[900px] text-sm">
			<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
				<tr><th class="px-4 py-3">Node</th><th class="px-4 py-3">Adresse</th><th class="px-4 py-3">Klasse</th><th class="px-4 py-3">Gerät</th><th class="px-4 py-3 text-right">IOMMU</th><th class="px-4 py-3">Durchgereicht an</th></tr>
			</thead>
			<tbody>
				{#each nodes as n (n.cluster + n.node)}
					{#each n.pci as p (p.id)}
						<tr class="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60" class:bg-accent-soft={p.assigned_to.length > 0}>
							<td class="px-4 py-2">{n.node}</td>
							<td class="px-4 py-2 font-mono text-xs">{p.id}</td>
							<td class="px-4 py-2 text-xs">{p.class_name ?? '–'}</td>
							<td class="px-4 py-2">{p.device_name ?? '–'}<div class="text-xs text-zinc-500">{p.vendor_name ?? ''}{p.subsystem ? ` · ${p.subsystem}` : ''}</div></td>
							<td class="tabular px-4 py-2 text-right">{p.iommu_group ?? '–'}</td>
							<td class="px-4 py-2">
								{#each p.assigned_to as a (a.vmid + a.key)}
									<a class="text-accent hover:underline" href="/guests/{a.cluster}/{a.vmid}">{a.vmid} {a.name}</a> <span class="text-xs text-zinc-500">({a.key})</span>
								{:else}<span class="text-zinc-500">–</span>{/each}
							</td>
						</tr>
					{/each}
				{/each}
			</tbody>
		</table>
	</div>
{:else}
	<div class="card overflow-x-auto p-0">
		<table class="w-full min-w-[1200px] text-sm">
			<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
				<tr><th class="px-3 py-3">Gast</th><th class="px-3 py-3">Node</th><th class="px-3 py-3">CPU</th><th class="px-3 py-3">RAM</th><th class="px-3 py-3">Disks</th><th class="px-3 py-3">Netz</th><th class="px-3 py-3">Firmware</th><th class="px-3 py-3">Agent / OS</th><th class="px-3 py-3">IPs</th></tr>
			</thead>
			<tbody>
				{#each guests as g (g.cluster + g.vmid)}
					<tr class="border-b border-zinc-100 align-top last:border-0 dark:border-zinc-800/60">
						<td class="px-3 py-2">
							<a href="/guests/{g.cluster}/{g.vmid}" class="font-medium hover:text-accent"><span class="text-zinc-500">{g.vmid}</span> {g.name}</a>
							<div class="text-[10px] text-zinc-500 uppercase">{g.type === 'qemu' ? 'VM' : 'LXC'} · {g.status}</div>
						</td>
						<td class="px-3 py-2">{g.node}</td>
						<td class="px-3 py-2 text-xs">{g.vcpus ?? g.cores ?? '–'} vCPU{#if g.cpu_type}<div class="text-zinc-500">{g.cpu_type}</div>{/if}</td>
						<td class="px-3 py-2 text-xs">{bytes(g.memory)}{#if g.balloon_min}<div class="text-zinc-500">Balloon ab {bytes(g.balloon_min)}</div>{/if}</td>
						<td class="px-3 py-2 font-mono text-xs">
							{#each g.disks.filter((d) => d.media !== 'cdrom' && !d.key.startsWith('unused')) as d (d.key)}
								<div>{d.key}: {d.storage ?? 'bind'} {d.size ? bytes(d.size, 0) : ''}</div>
							{/each}
						</td>
						<td class="px-3 py-2 font-mono text-xs">
							{#each g.nics as n (n.key)}<div>{n.bridge}{n.vlan ? `.${n.vlan}` : ''} <span class="text-zinc-500">{n.model}</span></div>{/each}
						</td>
						<td class="px-3 py-2 text-xs">{g.type === 'qemu' ? `${g.bios ?? ''} · ${g.machine ?? ''}` : g.unprivileged ? 'unprivilegiert' : 'privilegiert'}{#if g.hostpci.length}<div class="text-accent">PCI: {g.hostpci.length}</div>{/if}</td>
						<td class="px-3 py-2 text-xs">
							{#if g.type === 'qemu'}
								{g.agent_enabled ? (g.agent_running ? '✓ Agent' : g.agent_running === false ? '✗ Agent aus' : 'Agent') : '–'}
							{/if}
							<div class="text-zinc-500">{g.os_name ?? g.ostype ?? ''}</div>
						</td>
						<td class="px-3 py-2 font-mono text-xs">{g.ip_addresses.join(', ') || '–'}</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
{/if}

{#if smart}
	<SmartDialog cluster={smart.cluster} node={smart.node} disk={smart.disk} onclose={() => (smart = null)} />
{/if}
