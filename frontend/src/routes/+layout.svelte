<script lang="ts">
	import '../app.css';
	import favicon from '#lib/assets/favicon.svg';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/state';
	import { setUnauthorizedHandler } from '#lib/api.js';
	import { auth, type Role } from '#lib/auth.svelte.js';
	import { live } from '#lib/live.svelte.js';
	import { timeAgo } from '#lib/format.js';
	import { getJson } from '#lib/api.js';
	import { jobViewer, taskViewer } from '#lib/toast.svelte.js';
	import JobLogDialog from '#lib/components/JobLogDialog.svelte';
	import TaskLogDialog from '#lib/components/TaskLogDialog.svelte';
	import type { AlertEvent } from '#lib/types.js';
	import Toasts from '#lib/components/Toasts.svelte';
	import type { LayoutProps } from './$types';

	let { children }: LayoutProps = $props();

	const nav: { href: string; label: string; icon: string; role?: Role }[] = [
		{ href: '/', label: 'Übersicht', icon: 'M3 12l9-9 9 9M5 10v10h14V10' },
		{ href: '/nodes', label: 'Nodes', icon: 'M4 5h16v5H4zM4 14h16v5H4zM8 7.5h.01M8 16.5h.01' },
		{ href: '/guests', label: 'Gäste', icon: 'M4 6h16M4 12h16M4 18h16' },
		{ href: '/analysis', label: 'Lastanalyse', icon: 'M4 20V10M10 20V4M16 20v-7M22 20H2' },
		{ href: '/inventory', label: 'Inventar', icon: 'M3 7l9-4 9 4-9 4-9-4zM3 7v10l9 4 9-4V7M12 11v10' },
		{ href: '/alerts', label: 'Alarme', icon: 'M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9M10.3 21a1.9 1.9 0 0 0 3.4 0' },
		{ href: '/tasks', label: 'Aufgaben', icon: 'M9 6h11M9 12h11M9 18h11M4 6l1 1 2-2M4 12l1 1 2-2M4 18l1 1 2-2' },
		{ href: '/audit', label: 'Audit-Log', icon: 'M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z' },
		{ href: '/admin/api-keys', label: 'API-Keys', icon: 'M15 7a4 4 0 1 1-3.9 5H8v3H5v-3H2V9h9.1A4 4 0 0 1 15 7z', role: 'admin' },
		{ href: '/admin/ssh', label: 'SSH', icon: 'M4 17l6-6-6-6M12 19h8', role: 'admin' },
		{ href: '/admin/users', label: 'Benutzer', icon: 'M16 19v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1M9 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6M22 19v-1a4 4 0 0 0-3-3.9M16 4.1a3 3 0 0 1 0 5.8', role: 'admin' }
	];
	const isLogin = $derived(page.url.pathname === '/login');

	let dark = $state(true);
	let now = $state(Date.now());
	let menuOpen = $state(false);

	onMount(() => {
		// Dark is the default; only an explicit choice switches to light.
		try {
			dark = localStorage.getItem('theme') !== 'light';
		} catch {
			/* storage blocked */
		}
		document.documentElement.classList.toggle('dark', dark);
		const toLogin = () => {
			live.stop();
			auth.set(null);
			if (page.url.pathname !== '/login') goto(`/login?next=${encodeURIComponent(page.url.pathname + page.url.search)}`);
		};
		setUnauthorizedHandler(toLogin);
		live.onlogout = toLogin;
		auth.load();
		const timer = setInterval(() => (now = Date.now()), 1000);
		return () => {
			clearInterval(timer);
			live.stop();
		};
	});

	// Gate: everything except /login needs a complete session; the stream starts after login.
	$effect(() => {
		if (!auth.checked || isLogin) return;
		if (!auth.ready) {
			goto(`/login?next=${encodeURIComponent(page.url.pathname + page.url.search)}`);
		} else {
			live.start();
		}
	});

	// Firing alerts for the badge in the navigation.
	let firing = $state<AlertEvent[]>([]);
	$effect(() => {
		if (!auth.ready) return;
		const load = () => getJson<AlertEvent[]>('/api/v1/alerts', { state: 'firing' }).then((a) => (firing = a)).catch(() => {});
		load();
		const t = setInterval(load, 30_000);
		return () => clearInterval(t);
	});
	const worst = $derived(firing.some((a) => a.severity === 'critical') ? 'bg-red-500' : 'bg-amber-500');
	const openAlerts = $derived(firing.filter((a) => a.severity !== 'info' && !a.acknowledged_by).length);

	async function logout() {
		live.stop();
		await auth.logout();
		goto('/login');
	}

	function toggleTheme() {
		dark = !dark;
		document.documentElement.classList.toggle('dark', dark);
		try {
			localStorage.setItem('theme', dark ? 'dark' : 'light');
		} catch {
			/* ignore */
		}
	}

	const isActive = (href: string) =>
		href === '/' ? page.url.pathname === '/' : page.url.pathname.startsWith(href);

	// Data older than three poll intervals counts as stale.
	const stale = $derived(
		!live.lastMessageAt || now - live.lastMessageAt > 3 * 1000 * (live.meta?.poll_interval ?? 10)
	);
	const newest = $derived(
		live.clusters
			.map((c) => c.updated_at)
			.filter(Boolean)
			.sort()
			.at(-1) ?? null
	);
</script>

<svelte:head>
	<link rel="icon" href={favicon} />
</svelte:head>

{#if isLogin}
	{@render children()}
{:else if !auth.ready}
	<div class="flex min-h-dvh items-center justify-center text-sm text-zinc-500">Lade …</div>
{:else}
<div class="flex min-h-dvh">
	<!-- Sidebar (desktop) / drawer (mobile) -->
	<aside
		class="fixed inset-y-0 left-0 z-30 w-56 -translate-x-full border-r border-zinc-200 bg-white transition-transform md:static md:translate-x-0 dark:border-zinc-800 dark:bg-zinc-900"
		class:translate-x-0={menuOpen}
	>
		<div class="flex h-14 items-center gap-2 px-4">
			<img src={favicon} alt="" class="size-7" />
			<span class="font-semibold">PVE Dashboard</span>
		</div>
		<nav class="flex flex-col gap-1 p-2">
			{#each nav.filter((n) => !n.role || auth.can(n.role)) as item (item.href)}
				<a
					href={item.href}
					onclick={() => (menuOpen = false)}
					class="flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors {isActive(
						item.href
					)
						? 'bg-accent-soft text-accent'
						: 'text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800'}"
				>
					<svg viewBox="0 0 24 24" class="size-4 fill-none stroke-current stroke-2" aria-hidden="true">
						<path d={item.icon} stroke-linecap="round" stroke-linejoin="round" />
					</svg>
					{item.label}
					{#if item.href === '/alerts' && openAlerts}
						<span class="ml-auto rounded-full px-1.5 text-[11px] font-semibold text-white {worst}">{openAlerts}</span>
					{/if}
				</a>
			{/each}
		</nav>
		<div class="absolute right-0 bottom-0 left-0 p-4 text-xs text-zinc-500">
			v{live.meta?.version ?? '–'}
			{#if live.meta?.read_only}· nur lesend{:else if live.meta?.dry_run}· Dry-Run{/if}
		</div>
	</aside>
	{#if menuOpen}
		<button
			class="fixed inset-0 z-20 bg-black/40 md:hidden"
			aria-label="Menü schließen"
			onclick={() => (menuOpen = false)}
		></button>
	{/if}

	<div class="flex min-w-0 flex-1 flex-col">
		<header
			class="sticky top-0 z-10 flex h-14 items-center gap-3 border-b border-zinc-200 bg-zinc-50/80 px-4 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80"
		>
			<button
				class="rounded-lg p-1.5 hover:bg-zinc-200 md:hidden dark:hover:bg-zinc-800"
				aria-label="Menü öffnen"
				onclick={() => (menuOpen = true)}
			>
				<svg viewBox="0 0 24 24" class="size-5 fill-none stroke-current stroke-2"
					><path d="M4 6h16M4 12h16M4 18h16" stroke-linecap="round" /></svg
				>
			</button>

			{#if live.clusters.length > 1}
				<select class="input" bind:value={live.selected} aria-label="Cluster auswählen">
					<option value={null}>Alle Cluster</option>
					{#each live.clusters as c (c.name)}
						<option value={c.name}>{c.name}</option>
					{/each}
				</select>
			{/if}

			{#if live.meta?.mock}
				<span
					class="rounded-md bg-violet-500/15 px-2 py-0.5 text-xs font-semibold text-violet-600 dark:text-violet-400"
					title="Synthetische Beispieldaten (PVE_MOCK=true)">MOCK</span
				>
			{/if}

			<div class="ml-auto flex items-center gap-3 text-xs text-zinc-500">
				<span class="flex items-center gap-1.5" title="Live-Verbindung zum Backend">
					<span
						class="size-2 rounded-full {live.connected && !stale
							? 'animate-pulse bg-emerald-500'
							: 'bg-red-500'}"
					></span>
					<span class="hidden sm:inline">
						{live.connected ? (stale ? 'veraltet' : 'live') : 'getrennt'} · {timeAgo(newest, now)}
					</span>
				</span>
				<a href="/account" class="flex items-center gap-1.5 rounded-lg px-2 py-1 hover:bg-zinc-200 dark:hover:bg-zinc-800" title="Mein Konto">
					<span class="font-medium text-zinc-700 dark:text-zinc-300">{auth.me?.username}</span>
					<span class="hidden rounded bg-zinc-200 px-1.5 py-0.5 text-[10px] uppercase sm:inline dark:bg-zinc-800">{auth.me?.role}</span>
				</a>
				<button class="rounded-lg px-2 py-1 hover:bg-zinc-200 dark:hover:bg-zinc-800" onclick={logout}>Abmelden</button>
				<button
					class="rounded-lg p-1.5 hover:bg-zinc-200 dark:hover:bg-zinc-800"
					onclick={toggleTheme}
					aria-label={dark ? 'Helles Design' : 'Dunkles Design'}
					title={dark ? 'Helles Design' : 'Dunkles Design'}
				>
					<svg viewBox="0 0 24 24" class="size-4 fill-none stroke-current stroke-2">
						{#if dark}
							<circle cx="12" cy="12" r="4" /><path
								d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
								stroke-linecap="round"
							/>
						{:else}
							<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" stroke-linejoin="round" />
						{/if}
					</svg>
				</button>
			</div>
		</header>

		<main class="mx-auto w-full max-w-[1600px] flex-1 p-4 md:p-6">
			{@render children()}
		</main>
	</div>
</div>
{/if}

<Toasts />
{#if jobViewer.current}
	<JobLogDialog id={jobViewer.current} onclose={() => jobViewer.close()} />
{/if}
{#if taskViewer.current}
	<TaskLogDialog cluster={taskViewer.current.cluster} upid={taskViewer.current.upid} onclose={() => taskViewer.close()} />
{/if}
