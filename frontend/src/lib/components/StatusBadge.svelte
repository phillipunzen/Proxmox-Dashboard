<script lang="ts">
	// Coloured pill for node/guest/cluster status.
	let { status, label }: { status: string; label?: string } = $props();

	const styles: Record<string, string> = {
		online: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
		running: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
		ok: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400',
		stopped: 'bg-zinc-500/15 text-zinc-500 dark:text-zinc-400',
		paused: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
		suspended: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
		warning: 'bg-amber-500/15 text-amber-600 dark:text-amber-400',
		offline: 'bg-red-500/15 text-red-600 dark:text-red-400',
		critical: 'bg-red-500/15 text-red-600 dark:text-red-400',
		unknown: 'bg-zinc-500/15 text-zinc-500'
	};
	const names: Record<string, string> = {
		online: 'online',
		running: 'läuft',
		stopped: 'gestoppt',
		paused: 'pausiert',
		suspended: 'suspendiert',
		offline: 'offline',
		unknown: 'unbekannt'
	};
</script>

<span
	class="inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium whitespace-nowrap {styles[
		status
	] ?? styles.unknown}"
>
	<span class="size-1.5 rounded-full bg-current"></span>
	{label ?? names[status] ?? status}
</span>
