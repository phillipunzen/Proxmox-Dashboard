<script lang="ts">
	import { page } from '$app/state';
	import { getJson } from '#lib/api.js';
	import { live } from '#lib/live.svelte.js';
	import { bytes, pct, rate, ratio, uptime } from '#lib/format.js';
	import HistoryCharts from '#lib/components/HistoryCharts.svelte';
	import Kpi from '#lib/components/Kpi.svelte';
	import RangePicker from '#lib/components/RangePicker.svelte';
	import StatusBadge from '#lib/components/StatusBadge.svelte';
	import type { GuestInventory, RangeKey } from '#lib/types.js';

	const clusterName = $derived(page.params.cluster ?? '');
	const vmid = $derived(Number(page.params.vmid));
	const guest = $derived(
		live.clusters.find((c) => c.name === clusterName)?.guests.find((g) => g.vmid === vmid)
	);
	let inv = $state<GuestInventory | null>(null);
	let invError = $state<string | null>(null);
	let range = $state<RangeKey>('24h');

	$effect(() => {
		const path = `/api/v1/inventory/clusters/${encodeURIComponent(clusterName)}/guests/${vmid}`;
		getJson<GuestInventory>(path)
			.then((d) => {
				inv = d;
				invError = null;
			})
			.catch((e) => (invError = e.message));
	});

	const running = $derived(guest?.status === 'running');
	const isVm = $derived(guest?.type === 'qemu');
</script>

<svelte:head><title>{guest?.name ?? vmid} · Proxmox Dashboard</title></svelte:head>

<a href="/guests" class="text-sm text-zinc-500 hover:text-accent">← Gäste</a>

{#if !guest}
	<div class="card mt-4 text-sm text-zinc-500">{live.clusters.length ? 'Gast nicht gefunden.' : 'Lade …'}</div>
{:else}
	<div class="mt-2 mb-4 flex flex-wrap items-center gap-3">
		<span class="rounded bg-zinc-200 px-1.5 py-0.5 text-xs font-semibold text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400">{isVm ? 'VM' : 'LXC'}</span>
		<h1 class="text-xl font-semibold"><span class="text-zinc-500">{guest.vmid}</span> {guest.name}</h1>
		<StatusBadge status={guest.status} />
		<span class="text-sm text-zinc-500">
			auf <a class="hover:text-accent" href="/nodes/{guest.cluster}/{guest.node}">{guest.node}</a> · {guest.cluster}
		</span>
		{#each guest.tags as t (t)}<span class="rounded bg-accent-soft px-1.5 py-0.5 text-xs text-accent">{t}</span>{/each}
		{#if guest.console_url && running}
			<a href={guest.console_url} target="_blank" rel="noopener noreferrer" class="ml-auto text-sm text-accent hover:underline">Konsole ↗</a>
		{/if}
	</div>

	<div class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
		<Kpi label="CPU" value={running ? pct(guest.cpu) : '–'} sub="{guest.maxcpu} vCPU · Host {pct(guest.cpu_host, 1)}" ratio={running ? guest.cpu : null} />
		<Kpi label="RAM" value={running ? bytes(guest.mem) : '–'} sub="von {bytes(guest.maxmem)}" ratio={running ? ratio(guest.mem, guest.maxmem) : null} warn={0.85} crit={0.95} />
		<Kpi label="Disk lesen" value={rate(guest.diskread_bps)} />
		<Kpi label="Disk schreiben" value={rate(guest.diskwrite_bps)} />
		<Kpi label="Netz ein / aus" value={rate(guest.netin_bps)} sub="↑ {rate(guest.netout_bps)}" />
		<Kpi label="Uptime" value={running ? uptime(guest.uptime) : '–'} sub={guest.lock ? `Lock: ${guest.lock}` : ''} />
	</div>

	{#if live.meta?.history}
		<div class="card mt-4">
			<div class="mb-3 flex items-center justify-between">
				<span class="label">Verlauf</span>
				<RangePicker bind:value={range} />
			</div>
			<HistoryCharts
				path="/api/v1/history/clusters/{encodeURIComponent(guest.cluster)}/guests/{guest.vmid}"
				{range}
				columns={2}
				charts={[
					{ title: 'CPU (bezogen auf eigene vCPUs)', unit: 'pct', max: 1, lines: [{ key: 'cpu', name: 'CPU' }] },
					{ title: 'RAM', unit: 'bytes', lines: [{ key: 'mem', name: 'RAM', color: '#3b82f6' }] },
					{ title: 'Disk-IO', unit: 'bps', lines: [{ key: 'diskread', name: 'Lesen', color: '#10b981' }, { key: 'diskwrite', name: 'Schreiben', color: '#eab308' }] },
					{ title: 'Netzwerk', unit: 'bps', lines: [{ key: 'netin', name: 'Eingang', color: '#14b8a6' }, { key: 'netout', name: 'Ausgang', color: '#a855f7' }] }
				]}
			/>
		</div>
	{/if}

	<!-- Configuration inventory -->
	{#if invError}
		<div class="card mt-4 text-sm text-zinc-500">Konfiguration noch nicht erfasst: {invError}</div>
	{:else if inv}
		<div class="mt-4 grid gap-4 lg:grid-cols-3">
			<div class="card text-sm">
				<span class="label">Konfiguration</span>
				<dl class="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
					<dt class="text-zinc-500">vCPU</dt>
					<dd>{inv.vcpus ?? inv.cores ?? '–'}{inv.sockets ? ` (${inv.sockets} Socket × ${inv.cores} Kerne)` : ''}{inv.cpu_limit ? ` · Limit ${inv.cpu_limit}` : ''}</dd>
					{#if isVm}<dt class="text-zinc-500">CPU-Typ</dt><dd>{inv.cpu_type ?? '–'}</dd>{/if}
					<dt class="text-zinc-500">RAM</dt>
					<dd>
						{bytes(inv.memory)}
						{#if inv.balloon_min != null}· {inv.balloon_min ? `Ballooning ab ${bytes(inv.balloon_min)}` : 'Ballooning aus'}{/if}
						{#if inv.swap != null}· Swap {bytes(inv.swap)}{/if}
					</dd>
					{#if isVm}
						<dt class="text-zinc-500">Firmware</dt><dd>{inv.bios === 'ovmf' ? 'UEFI (OVMF)' : 'BIOS (SeaBIOS)'} · {inv.machine}</dd>
						<dt class="text-zinc-500">Guest Agent</dt>
						<dd>{inv.agent_enabled ? (inv.agent_running ? '✓ läuft' : inv.agent_running === false ? '✗ nicht erreichbar' : 'aktiviert') : 'nicht aktiviert'}</dd>
					{:else}
						<dt class="text-zinc-500">Container</dt><dd>{inv.unprivileged ? 'unprivilegiert' : 'privilegiert'} · {inv.arch}</dd>
					{/if}
					<dt class="text-zinc-500">OS</dt><dd>{inv.os_name ?? inv.ostype ?? '–'}</dd>
					<dt class="text-zinc-500">IP-Adressen</dt><dd class="font-mono text-xs">{inv.ip_addresses.join(', ') || '–'}</dd>
					<dt class="text-zinc-500">Autostart</dt><dd>{inv.onboot ? 'ja' : 'nein'}{inv.protection ? ' · geschützt' : ''}</dd>
				</dl>
			</div>

			<div class="card text-sm lg:col-span-2">
				<span class="label">Datenträger</span>
				<table class="mt-2 w-full text-sm">
					<tbody>
						{#each inv.disks as d (d.key)}
							<tr class="border-t border-zinc-100 first:border-0 dark:border-zinc-800/60">
								<td class="py-1.5 pr-3 font-mono text-xs">{d.key}</td>
								<td class="py-1.5 pr-3">{d.storage ?? 'Bind-Mount'}</td>
								<td class="py-1.5 pr-3 font-mono text-xs text-zinc-500 break-all">{d.volume}</td>
								<td class="tabular py-1.5 pr-3 text-right">{d.size ? bytes(d.size, 0) : ''}</td>
								<td class="py-1.5 text-xs text-zinc-500">{d.media === 'cdrom' ? 'CD-ROM' : d.mountpoint ?? ''}{d.key.startsWith('unused') ? 'unbenutzt' : ''}</td>
							</tr>
						{/each}
					</tbody>
				</table>

				<span class="label mt-5 block">Netzwerk</span>
				<table class="mt-2 w-full text-sm">
					<tbody>
						{#each inv.nics as n (n.key)}
							<tr class="border-t border-zinc-100 first:border-0 dark:border-zinc-800/60">
								<td class="py-1.5 pr-3 font-mono text-xs">{n.key}{n.name ? ` (${n.name})` : ''}</td>
								<td class="py-1.5 pr-3">{n.bridge ?? '–'}{n.vlan ? ` · VLAN ${n.vlan}` : ''}</td>
								<td class="py-1.5 pr-3 text-xs text-zinc-500">{n.model}</td>
								<td class="py-1.5 pr-3 font-mono text-xs">{n.mac}</td>
								<td class="py-1.5 text-xs text-zinc-500">{n.firewall ? 'Firewall' : ''}{n.rate_mbps ? ` · ${n.rate_mbps} Mbit/s` : ''}{n.ip_config ? ` · ${n.ip_config}` : ''}</td>
							</tr>
						{/each}
					</tbody>
				</table>

				{#if inv.hostpci.length || inv.usb.length}
					<span class="label mt-5 block">Durchgereichte Geräte</span>
					<ul class="mt-2 space-y-1 font-mono text-xs">
						{#each inv.hostpci as p (p.key)}
							<li>{p.key}: {p.mapping ? `Mapping „${p.mapping}“ → ` : ''}{p.devices.join(', ')}</li>
						{/each}
						{#each inv.usb as u (u)}<li>{u}</li>{/each}
					</ul>
				{/if}
			</div>
		</div>
		{#if inv.notes}
			<div class="card mt-4 text-sm">
				<span class="label">Notizen</span>
				<pre class="mt-2 font-sans whitespace-pre-wrap">{inv.notes}</pre>
			</div>
		{/if}
	{/if}
{/if}
