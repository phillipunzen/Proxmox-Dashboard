<script lang="ts">
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { onMount } from 'svelte';
	import favicon from '#lib/assets/favicon.svg';
	import { send } from '#lib/api.js';
	import { auth, type Me } from '#lib/auth.svelte.js';
	import TotpSetup from '#lib/components/TotpSetup.svelte';

	let username = $state('');
	let password = $state('');
	let code = $state('');
	let newPassword = $state('');
	let newPassword2 = $state('');
	let error = $state<string | null>(null);
	let busy = $state(false);

	// Only allow local redirect targets.
	const next = $derived.by(() => {
		const n = page.url.searchParams.get('next') ?? '/';
		return n.startsWith('/') && !n.startsWith('//') && !n.startsWith('/login') ? n : '/';
	});
	const step = $derived(auth.me?.pending ?? (auth.me ? 'done' : 'credentials'));

	onMount(async () => {
		if (!auth.checked) await auth.load();
	});

	$effect(() => {
		if (step === 'done') {
			auth.loadCapabilities();
			goto(next, { replaceState: true });
		}
	});

	async function attempt(fn: () => Promise<void>) {
		busy = true;
		error = null;
		try {
			await fn();
		} catch (e) {
			error = (e as Error).message;
		} finally {
			busy = false;
		}
	}

	const submitLogin = (ev: Event) => {
		ev.preventDefault();
		attempt(async () => {
			const r = await send<{ status: string; me: Me }>('POST', '/api/v1/auth/login', { username, password });
			password = '';
			auth.set(r.me);
		});
	};

	const submitCode = (ev: Event) => {
		ev.preventDefault();
		attempt(async () => {
			try {
				auth.set(await send<Me>('POST', '/api/v1/auth/totp/verify', { code }));
			} finally {
				code = '';
			}
		});
	};

	const submitPassword = (ev: Event) => {
		ev.preventDefault();
		if (newPassword !== newPassword2) {
			error = 'Die Passwörter stimmen nicht überein';
			return;
		}
		attempt(async () => {
			auth.set(await send<Me>('POST', '/api/v1/auth/password', { current: password, new: newPassword }));
		});
	};

	async function cancel() {
		await auth.logout();
		error = null;
	}
</script>

<svelte:head><title>Anmelden · Proxmox Dashboard</title></svelte:head>

<div class="flex min-h-dvh items-center justify-center p-4">
	<div class="card w-full max-w-sm p-6">
		<div class="mb-6 flex items-center gap-3">
			<img src={favicon} alt="" class="size-9" />
			<div>
				<div class="font-semibold">PVE Dashboard</div>
				<div class="text-xs text-zinc-500">
					{#if step === 'credentials'}Anmelden{:else if step === 'totp'}Zwei-Faktor-Code{:else if step === 'totp_setup'}Zwei-Faktor einrichten{:else if step === 'password_change'}Neues Passwort festlegen{/if}
				</div>
			</div>
		</div>

		{#if step === 'credentials'}
			<form onsubmit={submitLogin} class="flex flex-col gap-3">
				<label class="flex flex-col gap-1 text-sm">
					Benutzername
					<!-- svelte-ignore a11y_autofocus -->
					<input class="input" bind:value={username} autocomplete="username" autofocus required />
				</label>
				<label class="flex flex-col gap-1 text-sm">
					Passwort
					<input class="input" type="password" bind:value={password} autocomplete="current-password" required />
				</label>
				<button class="mt-2 rounded-lg bg-accent py-2 text-sm font-medium text-white disabled:opacity-50" disabled={busy}>
					{busy ? 'Prüfe …' : 'Anmelden'}
				</button>
			</form>
		{:else if step === 'totp'}
			<form onsubmit={submitCode} class="flex flex-col gap-3">
				<p class="text-sm text-zinc-500">Code aus der Authenticator-App für <strong>{auth.me?.username}</strong>:</p>
				<!-- svelte-ignore a11y_autofocus -->
				<input class="input text-center font-mono text-xl tracking-[0.4em]" bind:value={code} inputmode="numeric" autocomplete="one-time-code" maxlength="6" placeholder="000000" autofocus aria-label="Code" />
				<button class="rounded-lg bg-accent py-2 text-sm font-medium text-white disabled:opacity-50" disabled={busy || code.length !== 6}>Bestätigen</button>
				<button type="button" class="text-xs text-zinc-500 hover:underline" onclick={cancel}>Anderer Benutzer</button>
			</form>
		{:else if step === 'totp_setup'}
			<p class="mb-3 rounded-lg bg-amber-500/10 px-3 py-2 text-xs text-amber-600 dark:text-amber-400">
				Für die Rolle „{auth.me?.role}“ ist die Zwei-Faktor-Anmeldung Pflicht.
			</p>
			<TotpSetup ondone={(me) => auth.set(me)} />
			<button type="button" class="mt-3 text-xs text-zinc-500 hover:underline" onclick={cancel}>Abbrechen</button>
		{:else if step === 'password_change'}
			<form onsubmit={submitPassword} class="flex flex-col gap-3">
				<p class="text-sm text-zinc-500">Du hast dich mit einem vorläufigen Passwort angemeldet. Bitte lege ein eigenes fest (mindestens 12 Zeichen).</p>
				<label class="flex flex-col gap-1 text-sm">
					Vorläufiges Passwort
					<input class="input" type="password" bind:value={password} autocomplete="current-password" required />
				</label>
				<label class="flex flex-col gap-1 text-sm">
					Neues Passwort
					<input class="input" type="password" bind:value={newPassword} autocomplete="new-password" minlength="12" required />
				</label>
				<label class="flex flex-col gap-1 text-sm">
					Wiederholen
					<input class="input" type="password" bind:value={newPassword2} autocomplete="new-password" minlength="12" required />
				</label>
				<button class="rounded-lg bg-accent py-2 text-sm font-medium text-white disabled:opacity-50" disabled={busy}>Speichern</button>
			</form>
		{/if}

		{#if error}<p class="mt-4 rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-600 dark:text-red-400" role="alert">{error}</p>{/if}
	</div>
</div>
