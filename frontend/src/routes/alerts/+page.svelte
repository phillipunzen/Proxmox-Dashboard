<script lang="ts">
	import { getJson, send } from '#lib/api.js';
	import { auth } from '#lib/auth.svelte.js';
	import { live } from '#lib/live.svelte.js';
	import { dateTime, timeAgo } from '#lib/format.js';
	import { toasts } from '#lib/toast.svelte.js';
	import ConfirmDialog from '#lib/components/ConfirmDialog.svelte';
	import Modal from '#lib/components/Modal.svelte';
	import StatusBadge from '#lib/components/StatusBadge.svelte';
	import type { AlertEvent, AlertRule, Channel, MetricInfo } from '#lib/types.js';

	type Tab = 'firing' | 'history' | 'rules' | 'channels';
	let tab = $state<Tab>('firing');
	let events = $state<AlertEvent[]>([]);
	let rules = $state<AlertRule[]>([]);
	let channels = $state<Channel[]>([]);
	let metrics = $state<MetricInfo[]>([]);
	let editRule = $state<AlertRule | null>(null);
	let editChannel = $state<Channel | null>(null);
	let remove = $state<{ kind: 'rule' | 'channel'; id: number; name: string } | null>(null);

	const isAdmin = $derived(auth.can('admin'));
	const metricOf = (k: string) => metrics.find((m) => m.key === k);

	async function load() {
		events = await getJson<AlertEvent[]>('/api/v1/alerts', { state: tab === 'history' ? 'all' : 'firing', limit: 300 });
		if (tab === 'rules' || tab === 'channels' || !metrics.length) {
			[rules, metrics] = await Promise.all([getJson<AlertRule[]>('/api/v1/alerts/rules'), getJson<MetricInfo[]>('/api/v1/alerts/metrics')]);
			if (isAdmin) channels = await getJson<Channel[]>('/api/v1/alerts/channels');
		}
	}

	$effect(() => {
		void tab;
		load();
		const t = setInterval(load, 15_000);
		return () => clearInterval(t);
	});

	async function ack(e: AlertEvent) {
		await send('POST', `/api/v1/alerts/${e.id}/ack`);
		load();
	}

	// --- rule editor: thresholds in the UI unit (percent for ratios) ---------------------------
	const isRatio = (m?: MetricInfo) => m?.unit === 'ratio';
	let uiThreshold = $state(0);

	function newRule() {
		const m = metrics[0];
		editRule = { name: '', enabled: true, metric: m.key, operator: m.default_operator as AlertRule['operator'], threshold: m.default_threshold, for_seconds: 0, severity: 'warning', selector: {}, channel_ids: [], repeat_minutes: 0 };
		uiThreshold = isRatio(m) ? m.default_threshold * 100 : m.default_threshold;
	}

	function openRule(r: AlertRule) {
		editRule = structuredClone($state.snapshot(r));
		uiThreshold = isRatio(metricOf(r.metric)) ? r.threshold * 100 : r.threshold;
	}

	function metricChanged() {
		if (!editRule) return;
		const m = metricOf(editRule.metric);
		if (!m) return;
		editRule.operator = m.default_operator as AlertRule['operator'];
		uiThreshold = isRatio(m) ? m.default_threshold * 100 : m.default_threshold;
	}

	async function saveRule() {
		if (!editRule) return;
		const m = metricOf(editRule.metric);
		const body = { ...editRule, threshold: isRatio(m) ? uiThreshold / 100 : uiThreshold };
		for (const k of Object.keys(body.selector)) if (body.selector[k] === '') delete body.selector[k];
		await send(editRule.id ? 'PUT' : 'POST', editRule.id ? `/api/v1/alerts/rules/${editRule.id}` : '/api/v1/alerts/rules', body);
		toasts.push('ok', 'Regel gespeichert');
		editRule = null;
		load();
	}

	async function toggle(r: AlertRule) {
		await send('PUT', `/api/v1/alerts/rules/${r.id}`, { ...r, enabled: !r.enabled });
		load();
	}

	// --- channels -----------------------------------------------------------------------------
	const FIELDS: Record<Channel['type'], { key: string; label: string; secret?: boolean; placeholder?: string }[]> = {
		ntfy: [{ key: 'url', label: 'Server', placeholder: 'https://ntfy.sh' }, { key: 'topic', label: 'Topic' }, { key: 'token', label: 'Access-Token (optional)', secret: true }],
		gotify: [{ key: 'url', label: 'Server', placeholder: 'https://gotify.lan' }, { key: 'token', label: 'App-Token', secret: true }],
		webhook: [{ key: 'url', label: 'URL' }, { key: 'secret', label: 'HMAC-Secret (optional)', secret: true }],
		homeassistant: [{ key: 'url', label: 'Home-Assistant-URL', placeholder: 'http://homeassistant.lan:8123' }, { key: 'webhook_id', label: 'Webhook-ID' }],
		email: [{ key: 'host', label: 'SMTP-Server' }, { key: 'port', label: 'Port', placeholder: '587' }, { key: 'security', label: 'Sicherheit (starttls | ssl | none)', placeholder: 'starttls' }, { key: 'username', label: 'Benutzer (optional)' }, { key: 'password', label: 'Passwort (optional)', secret: true }, { key: 'sender', label: 'Absender' }, { key: 'recipients', label: 'Empfänger (Komma-getrennt)' }]
	};

	function newChannel() {
		editChannel = { name: '', type: 'ntfy', config: { url: 'https://ntfy.sh' }, enabled: true, min_severity: 'warning', send_resolved: true };
	}

	async function saveChannel() {
		if (!editChannel) return;
		const c = $state.snapshot(editChannel);
		await send(c.id ? ('PUT') : 'POST', c.id ? `/api/v1/alerts/channels/${c.id}` : '/api/v1/alerts/channels', c);
		toasts.push('ok', 'Kanal gespeichert');
		editChannel = null;
		load();
	}

	async function test(c: Channel) {
		const r = await send<{ ok: boolean; message: string }>('POST', `/api/v1/alerts/channels/${c.id}/test`);
		toasts.push(r.ok ? 'ok' : 'error', `${c.name}: ${r.message}`);
	}

	const sev = (s: string) => (s === 'critical' ? 'critical' : s === 'warning' ? 'warning' : 'unknown');
	const sevLabel = (s: string) => ({ critical: 'kritisch', warning: 'Warnung', info: 'Info' })[s] ?? s;
	const fmtThreshold = (r: AlertRule) => {
		const m = metricOf(r.metric);
		if (!m) return `${r.operator} ${r.threshold}`;
		if (m.unit === 'bool') return '';
		if (m.unit === 'ratio') return `${r.operator} ${Math.round(r.threshold * 100)} %`;
		if (m.unit === 'celsius') return `${r.operator} ${r.threshold} °C`;
		if (m.unit === 'percent') return `${r.operator} ${r.threshold} %`;
		return `${r.operator} ${r.threshold}`;
	};
</script>

<svelte:head><title>Alarme · Proxmox Dashboard</title></svelte:head>

<h1 class="mb-4 text-xl font-semibold">Alarme</h1>

<div class="mb-4 flex flex-wrap gap-1 border-b border-zinc-200 dark:border-zinc-800" role="tablist">
	{#each [['firing', 'Aktiv'], ['history', 'Verlauf'], ['rules', 'Regeln'], ['channels', 'Benachrichtigung']] as [k, label] (k)}
		{#if k !== 'channels' || isAdmin}
			<button role="tab" aria-selected={tab === k}
				class="-mb-px border-b-2 px-3 py-2 text-sm font-medium {tab === k ? 'border-accent text-accent' : 'border-transparent text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200'}"
				onclick={() => (tab = k as Tab)}>{label}{#if k === 'firing' && tab !== 'firing'}{''}{/if}</button>
		{/if}
	{/each}
</div>

{#if tab === 'firing' || tab === 'history'}
	<div class="card overflow-x-auto p-0">
		<table class="w-full min-w-[860px] text-sm">
			<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
				<tr><th class="px-4 py-3">Schwere</th><th class="px-4 py-3">Regel</th><th class="px-4 py-3">Meldung</th><th class="px-4 py-3">Seit</th><th class="px-4 py-3">Status</th><th></th></tr>
			</thead>
			<tbody>
				{#each events as e (e.id)}
					<tr class="border-b border-zinc-100 align-top last:border-0 dark:border-zinc-800/60">
						<td class="px-4 py-2"><StatusBadge status={sev(e.severity)} label={sevLabel(e.severity)} /></td>
						<td class="px-4 py-2 font-medium">{e.rule_name}</td>
						<td class="px-4 py-2">{e.message}<div class="font-mono text-xs text-zinc-500">{e.target}</div></td>
						<td class="px-4 py-2 text-xs whitespace-nowrap">{dateTime(Date.parse(e.started_at) / 1000)}<div class="text-zinc-500">{timeAgo(e.started_at)}</div></td>
						<td class="px-4 py-2 text-xs">
							{#if e.state === 'resolved'}<span class="text-emerald-500">behoben {e.resolved_at ? dateTime(Date.parse(e.resolved_at) / 1000) : ''}</span>
							{:else}<span class="text-red-500">aktiv</span>{/if}
							{#if e.acknowledged_by}<div class="text-zinc-500">bestätigt: {e.acknowledged_by}</div>{/if}
						</td>
						<td class="px-4 py-2 text-right">
							{#if e.state === 'firing' && !e.acknowledged_by && auth.can('operator')}<button class="text-xs text-accent hover:underline" onclick={() => ack(e)}>Bestätigen</button>{/if}
						</td>
					</tr>
				{:else}
					<tr><td colspan="6" class="px-4 py-8 text-center text-emerald-500">{tab === 'firing' ? '✓ Keine aktiven Alarme' : 'Keine Einträge'}</td></tr>
				{/each}
			</tbody>
		</table>
	</div>
{:else if tab === 'rules'}
	{#if isAdmin}<button class="mb-3 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white" onclick={newRule}>Neue Regel</button>{/if}
	<div class="card overflow-x-auto p-0">
		<table class="w-full min-w-[900px] text-sm">
			<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
				<tr><th class="px-4 py-3">Aktiv</th><th class="px-4 py-3">Regel</th><th class="px-4 py-3">Bedingung</th><th class="px-4 py-3">Dauer</th><th class="px-4 py-3">Schwere</th><th class="px-4 py-3">Kanäle</th><th></th></tr>
			</thead>
			<tbody>
				{#each rules as r (r.id)}
					<tr class="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60" class:opacity-50={!r.enabled}>
						<td class="px-4 py-2"><input type="checkbox" checked={r.enabled} disabled={!isAdmin} onchange={() => toggle(r)} aria-label="aktiv" /></td>
						<td class="px-4 py-2 font-medium">{r.name}{#if Object.keys(r.selector).length}<div class="font-mono text-xs text-zinc-500">{Object.entries(r.selector).map(([k, v]) => `${k}=${v}`).join(' ')}</div>{/if}</td>
						<td class="px-4 py-2">{metricOf(r.metric)?.label ?? r.metric} {fmtThreshold(r)}</td>
						<td class="px-4 py-2 text-xs">{r.for_seconds ? `${r.for_seconds} s` : 'sofort'}{r.repeat_minutes ? ` · alle ${r.repeat_minutes} min` : ''}</td>
						<td class="px-4 py-2"><StatusBadge status={sev(r.severity)} label={sevLabel(r.severity)} /></td>
						<td class="px-4 py-2 text-xs">{r.channel_ids.map((id) => channels.find((c) => c.id === id)?.name ?? `#${id}`).join(', ') || '–'}</td>
						<td class="px-4 py-2 text-right text-xs whitespace-nowrap">
							{#if isAdmin}<button class="hover:text-accent" onclick={() => openRule(r)}>Bearbeiten</button> · <button class="text-red-500 hover:underline" onclick={() => (remove = { kind: 'rule', id: r.id!, name: r.name })}>Löschen</button>{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
	{#if !channels.length && isAdmin}<p class="mt-3 text-xs text-amber-500">Noch kein Benachrichtigungskanal – Alarme erscheinen nur hier im Dashboard.</p>{/if}
{:else if tab === 'channels'}
	<button class="mb-3 rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white" onclick={newChannel}>Neuer Kanal</button>
	<div class="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
		{#each channels as c (c.id)}
			<div class="card text-sm" class:opacity-50={!c.enabled}>
				<div class="flex items-center justify-between"><span class="font-medium">{c.name}</span><span class="rounded bg-zinc-200 px-1.5 text-xs uppercase dark:bg-zinc-800">{c.type}</span></div>
				<div class="mt-2 font-mono text-xs break-all text-zinc-500">{c.config.url ?? c.config.host ?? ''}{c.config.topic ? `/${c.config.topic}` : ''}</div>
				<div class="mt-1 text-xs text-zinc-500">ab {sevLabel(c.min_severity)}{c.send_resolved ? ' · inkl. Entwarnung' : ''}</div>
				<div class="mt-3 flex gap-3 text-xs">
					<button class="text-accent hover:underline" onclick={() => test(c)}>Test senden</button>
					<button class="hover:text-accent" onclick={() => (editChannel = structuredClone($state.snapshot(c)))}>Bearbeiten</button>
					<button class="text-red-500 hover:underline" onclick={() => (remove = { kind: 'channel', id: c.id!, name: c.name })}>Löschen</button>
				</div>
			</div>
		{:else}<p class="text-sm text-zinc-500">Noch keine Kanäle.</p>{/each}
	</div>
{/if}

{#if editRule}
	{@const m = metricOf(editRule.metric)}
	<Modal title={editRule.id ? 'Regel bearbeiten' : 'Neue Regel'} onclose={() => (editRule = null)} width="640px">
		<form id="rule-form" class="grid gap-3" onsubmit={(e) => { e.preventDefault(); saveRule().catch((err) => toasts.push('error', err.message)); }}>
			<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Name</span><input class="input" bind:value={editRule.name} required /></label>
			<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Metrik</span>
				<select class="input" bind:value={editRule.metric} onchange={metricChanged}>{#each metrics as mi (mi.key)}<option value={mi.key}>{mi.label}</option>{/each}</select>
			</label>
			{#if m && m.unit !== 'bool'}
				<div class="flex gap-2">
					<select class="input" bind:value={editRule.operator}>{#each ['>', '>=', '<', '<='] as o (o)}<option>{o}</option>{/each}</select>
					<input class="input flex-1" type="number" step="any" bind:value={uiThreshold} />
					<span class="self-center text-sm text-zinc-500">{({ ratio: '%', celsius: '°C', percent: '%', count: 'Stück' } as Record<string, string>)[m.unit] ?? ''}</span>
				</div>
			{/if}
			<div class="grid grid-cols-2 gap-2">
				<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Mindestdauer (s)</span><input class="input" type="number" min="0" bind:value={editRule.for_seconds} /></label>
				<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Wiederholen alle (min, 0 = nie)</span><input class="input" type="number" min="0" bind:value={editRule.repeat_minutes} /></label>
			</div>
			<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Schwere</span>
				<select class="input" bind:value={editRule.severity}><option value="info">Info</option><option value="warning">Warnung</option><option value="critical">kritisch</option></select>
			</label>
			<fieldset class="grid grid-cols-2 gap-2">
				<legend class="mb-1 text-xs text-zinc-500">Einschränken (optional)</legend>
				<select class="input" bind:value={editRule.selector.cluster}><option value="">alle Cluster</option>{#each live.clusters as c (c.name)}<option value={c.name}>{c.name}</option>{/each}</select>
				<input class="input" placeholder="Node" bind:value={editRule.selector.node} />
				<input class="input" placeholder="Tag (Gäste)" bind:value={editRule.selector.tag} />
				<input class="input" placeholder="VMID / Storage" bind:value={editRule.selector[m?.kind === 'storage' ? 'storage' : 'vmid']} />
			</fieldset>
			<fieldset>
				<legend class="mb-1 text-xs text-zinc-500">Benachrichtigen über</legend>
				{#each channels as c (c.id)}
					<label class="mr-4 inline-flex items-center gap-1"><input type="checkbox" checked={editRule.channel_ids.includes(c.id!)}
						onchange={(ev) => { const on = (ev.target as HTMLInputElement).checked; editRule!.channel_ids = on ? [...editRule!.channel_ids, c.id!] : editRule!.channel_ids.filter((x) => x !== c.id); }} /> {c.name}</label>
				{:else}<span class="text-xs text-zinc-500">Keine Kanäle angelegt</span>{/each}
			</fieldset>
		</form>
		{#snippet footer()}
			<button class="input" onclick={() => (editRule = null)}>Abbrechen</button>
			<button form="rule-form" class="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white">Speichern</button>
		{/snippet}
	</Modal>
{/if}

{#if editChannel}
	<Modal title={editChannel.id ? 'Kanal bearbeiten' : 'Neuer Kanal'} onclose={() => (editChannel = null)}>
		<form id="ch-form" class="grid gap-3" onsubmit={(e) => { e.preventDefault(); saveChannel().catch((err) => toasts.push('error', err.message)); }}>
			<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Name</span><input class="input" bind:value={editChannel.name} required /></label>
			<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Typ</span>
				<select class="input" bind:value={editChannel.type} disabled={!!editChannel.id}>
					<option value="ntfy">ntfy</option><option value="gotify">Gotify</option><option value="email">E-Mail (SMTP)</option><option value="webhook">Webhook</option><option value="homeassistant">Home Assistant (Webhook)</option>
				</select>
			</label>
			{#each FIELDS[editChannel.type] as f (f.key)}
				<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">{f.label}</span>
					<input class="input" type={f.secret ? 'password' : 'text'} placeholder={f.placeholder ?? ''} autocomplete="off"
						value={Array.isArray(editChannel.config[f.key]) ? (editChannel.config[f.key] as string[]).join(', ') : (editChannel.config[f.key] ?? '')}
						oninput={(ev) => (editChannel!.config[f.key] = (ev.target as HTMLInputElement).value)} />
				</label>
			{/each}
			<div class="grid grid-cols-2 gap-2">
				<label class="flex flex-col gap-1"><span class="text-xs text-zinc-500">Ab Schwere</span>
					<select class="input" bind:value={editChannel.min_severity}><option value="info">Info</option><option value="warning">Warnung</option><option value="critical">kritisch</option></select>
				</label>
				<label class="flex items-end gap-2 pb-2"><input type="checkbox" bind:checked={editChannel.send_resolved} /> Entwarnung senden</label>
			</div>
			<label class="flex items-center gap-2"><input type="checkbox" bind:checked={editChannel.enabled} /> aktiv</label>
		</form>
		{#snippet footer()}
			<button class="input" onclick={() => (editChannel = null)}>Abbrechen</button>
			<button form="ch-form" class="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-white">Speichern</button>
		{/snippet}
	</Modal>
{/if}

{#if remove}
	{@const r = remove}
	<ConfirmDialog title="„{r.name}“ löschen?" message={r.kind === 'rule' ? 'Aktive Alarme dieser Regel werden aufgelöst.' : 'Regeln verweisen danach nicht mehr auf diesen Kanal.'}
		danger confirmLabel="Löschen" onclose={() => (remove = null)}
		onconfirm={async () => { await send('DELETE', `/api/v1/alerts/${r.kind === 'rule' ? 'rules' : 'channels'}/${r.id}`); load(); }} />
{/if}
