<script lang="ts">
	import { onMount } from 'svelte';
	import { getJson, send } from '#lib/api.js';
	import { auth, type Me } from '#lib/auth.svelte.js';
	import { dateTime } from '#lib/format.js';
	import { toasts } from '#lib/toast.svelte.js';
	import TotpSetup from '#lib/components/TotpSetup.svelte';

	interface SessionView {
		id: string;
		current: boolean;
		created_at: string;
		last_seen_at: string;
		ip: string | null;
		user_agent: string | null;
	}

	let current = $state('');
	let next = $state('');
	let next2 = $state('');
	let pwError = $state<string | null>(null);
	let showSetup = $state(false);
	let disablePw = $state('');
	let disableCode = $state('');
	let sessions = $state<SessionView[]>([]);

	const loadSessions = async () => (sessions = await getJson<SessionView[]>('/api/v1/auth/sessions'));
	onMount(loadSessions);

	async function changePassword(ev: Event) {
		ev.preventDefault();
		pwError = null;
		if (next !== next2) {
			pwError = 'Die Passwörter stimmen nicht überein';
			return;
		}
		try {
			auth.set(await send<Me>('POST', '/api/v1/auth/password', { current, new: next }));
			current = next = next2 = '';
			toasts.push('ok', 'Passwort geändert – andere Sitzungen wurden beendet');
			await loadSessions();
		} catch (e) {
			pwError = (e as Error).message;
		}
	}

	async function disableTotp(ev: Event) {
		ev.preventDefault();
		try {
			await send('POST', '/api/v1/auth/totp/disable', { password: disablePw, code: disableCode });
			await auth.load();
			toasts.push('ok', 'Zwei-Faktor-Anmeldung deaktiviert');
		} catch (e) {
			toasts.push('error', (e as Error).message);
		} finally {
			disablePw = disableCode = '';
		}
	}

	async function revoke(id: string) {
		await send('DELETE', `/api/v1/auth/sessions/${id}`);
		await loadSessions();
	}

	const browser = (ua: string | null) =>
		!ua ? '–' : (ua.match(/(Firefox|Edg|Chrome|Safari)\/[\d.]+/)?.[0] ?? ua.slice(0, 40));
</script>

<svelte:head><title>Mein Konto · Proxmox Dashboard</title></svelte:head>

<h1 class="mb-4 text-xl font-semibold">Mein Konto</h1>

<div class="grid gap-4 lg:grid-cols-2">
	<div class="card">
		<span class="label">Passwort ändern</span>
		<form onsubmit={changePassword} class="mt-3 flex flex-col gap-3 text-sm">
			<input class="input" type="password" placeholder="Aktuelles Passwort" bind:value={current} autocomplete="current-password" required />
			<input class="input" type="password" placeholder="Neues Passwort (min. 12 Zeichen)" bind:value={next} autocomplete="new-password" minlength="12" required />
			<input class="input" type="password" placeholder="Neues Passwort wiederholen" bind:value={next2} autocomplete="new-password" minlength="12" required />
			{#if pwError}<p class="text-red-500">{pwError}</p>{/if}
			<button class="self-start rounded-lg bg-accent px-3 py-1.5 font-medium text-white">Speichern</button>
		</form>
	</div>

	<div class="card text-sm">
		<span class="label">Zwei-Faktor-Anmeldung (TOTP)</span>
		{#if auth.me?.totp_enabled}
			<p class="mt-3 text-emerald-600 dark:text-emerald-400">✓ Aktiv</p>
			{#if auth.me.totp_required}
				<p class="mt-2 text-zinc-500">Für deine Rolle ist TOTP Pflicht. Bei Verlust des Geräts setzt ein Admin es zurück.</p>
			{:else}
				<form onsubmit={disableTotp} class="mt-3 flex flex-col gap-2">
					<p class="text-zinc-500">Deaktivieren:</p>
					<input class="input" type="password" placeholder="Passwort" bind:value={disablePw} required />
					<input class="input font-mono" placeholder="Aktueller Code" bind:value={disableCode} inputmode="numeric" maxlength="6" required />
					<button class="self-start rounded-lg border border-red-500/50 px-3 py-1.5 text-red-500">Deaktivieren</button>
				</form>
			{/if}
		{:else if showSetup}
			<div class="mt-3"><TotpSetup ondone={(me) => { auth.set(me); showSetup = false; toasts.push('ok', 'TOTP aktiviert'); loadSessions(); }} /></div>
		{:else}
			<p class="mt-3 text-zinc-500">Nicht aktiv. Empfohlen für alle Konten.</p>
			<button class="mt-3 rounded-lg bg-accent px-3 py-1.5 font-medium text-white" onclick={() => (showSetup = true)}>Einrichten</button>
		{/if}
	</div>

	<div class="card overflow-x-auto p-0 lg:col-span-2">
		<div class="label px-4 pt-4">Aktive Sitzungen</div>
		<table class="mt-2 w-full min-w-[640px] text-sm">
			<thead class="text-left text-xs text-zinc-500">
				<tr><th class="px-4 py-2">Browser</th><th class="px-4 py-2">IP</th><th class="px-4 py-2">Angemeldet</th><th class="px-4 py-2">Zuletzt aktiv</th><th></th></tr>
			</thead>
			<tbody>
				{#each sessions as s (s.id)}
					<tr class="border-t border-zinc-100 dark:border-zinc-800/60">
						<td class="px-4 py-2">{browser(s.user_agent)}</td>
						<td class="px-4 py-2 font-mono text-xs">{s.ip ?? '–'}</td>
						<td class="px-4 py-2">{dateTime(Date.parse(s.created_at) / 1000)}</td>
						<td class="px-4 py-2">{dateTime(Date.parse(s.last_seen_at) / 1000)}</td>
						<td class="px-4 py-2 text-right">
							{#if s.current}<span class="text-xs text-emerald-500">diese Sitzung</span>{:else}<button class="text-xs text-red-500 hover:underline" onclick={() => revoke(s.id)}>Beenden</button>{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
</div>
