<script lang="ts">
	import { onMount } from 'svelte';
	import { getJson, send } from '#lib/api.js';
	import { auth, type Role } from '#lib/auth.svelte.js';
	import { dateTime } from '#lib/format.js';
	import { toasts } from '#lib/toast.svelte.js';
	import ConfirmDialog from '#lib/components/ConfirmDialog.svelte';
	import Modal from '#lib/components/Modal.svelte';

	interface UserView {
		id: number;
		username: string;
		role: Role;
		totp_enabled: boolean;
		disabled: boolean;
		must_change_password: boolean;
		locked: boolean;
		created_at: string;
		last_login_at: string | null;
	}

	let users = $state<UserView[]>([]);
	let newName = $state('');
	let newRole = $state<Role>('viewer');
	let temp = $state<{ username: string; password: string } | null>(null);
	let confirm = $state<{ title: string; message: string; run: () => Promise<void>; danger?: boolean; text?: string } | null>(null);

	const load = async () => (users = await getJson<UserView[]>('/api/v1/admin/users'));
	onMount(load);

	async function act(fn: () => Promise<unknown>, ok: string) {
		try {
			await fn();
			toasts.push('ok', ok);
		} catch (e) {
			toasts.push('error', (e as Error).message);
		}
		await load();
	}

	async function create(ev: Event) {
		ev.preventDefault();
		try {
			const r = await send<{ user: UserView; temporary_password: string }>('POST', '/api/v1/admin/users', { username: newName, role: newRole });
			temp = { username: r.user.username, password: r.temporary_password };
			newName = '';
			await load();
		} catch (e) {
			toasts.push('error', (e as Error).message);
		}
	}

	function resetPassword(u: UserView) {
		confirm = {
			title: `Passwort von ${u.username} zurücksetzen?`,
			message: 'Es wird ein vorläufiges Passwort erzeugt, alle Sitzungen des Benutzers werden beendet.',
			run: async () => {
				const r = await send<{ temporary_password: string }>('POST', `/api/v1/admin/users/${u.id}/reset-password`);
				temp = { username: u.username, password: r.temporary_password };
				await load();
			}
		};
	}

	function resetTotp(u: UserView) {
		confirm = {
			title: `TOTP von ${u.username} zurücksetzen?`,
			message: 'Der Benutzer muss die Zwei-Faktor-Anmeldung neu einrichten (z. B. nach Verlust des Handys).',
			run: () => act(() => send('POST', `/api/v1/admin/users/${u.id}/reset-totp`), 'TOTP zurückgesetzt')
		};
	}

	function remove(u: UserView) {
		confirm = {
			title: `Benutzer ${u.username} löschen?`,
			message: 'Der Benutzer wird endgültig gelöscht. Einträge im Audit-Log bleiben erhalten.',
			danger: true,
			text: u.username,
			run: () => act(() => send('DELETE', `/api/v1/admin/users/${u.id}`), 'Benutzer gelöscht')
		};
	}
</script>

<svelte:head><title>Benutzer · Proxmox Dashboard</title></svelte:head>

<h1 class="mb-4 text-xl font-semibold">Benutzer</h1>

<form onsubmit={create} class="card mb-4 flex flex-wrap items-end gap-2 text-sm">
	<label class="flex flex-col gap-1">
		<span class="label">Neuer Benutzer</span>
		<input class="input" bind:value={newName} placeholder="benutzername" pattern="[A-Za-z0-9._@\-]+" required />
	</label>
	<select class="input" bind:value={newRole} aria-label="Rolle">
		<option value="viewer">Viewer – nur lesen</option>
		<option value="operator">Operator – Gäste steuern</option>
		<option value="admin">Admin – alles inkl. Benutzer</option>
	</select>
	<button class="rounded-lg bg-accent px-3 py-1.5 font-medium text-white">Anlegen</button>
	<span class="text-xs text-zinc-500">Ein vorläufiges Passwort wird erzeugt und muss beim ersten Login geändert werden.</span>
</form>

<div class="card overflow-x-auto p-0">
	<table class="w-full min-w-[860px] text-sm">
		<thead class="label border-b border-zinc-200 text-left dark:border-zinc-800">
			<tr><th class="px-4 py-3">Benutzer</th><th class="px-4 py-3">Rolle</th><th class="px-4 py-3">TOTP</th><th class="px-4 py-3">Status</th><th class="px-4 py-3">Letzter Login</th><th class="px-4 py-3"></th></tr>
		</thead>
		<tbody>
			{#each users as u (u.id)}
				{@const self = u.username === auth.me?.username}
				<tr class="border-b border-zinc-100 last:border-0 dark:border-zinc-800/60">
					<td class="px-4 py-2 font-medium">{u.username}{#if self}<span class="ml-1 text-xs text-zinc-500">(du)</span>{/if}</td>
					<td class="px-4 py-2">
						<select class="input py-1" value={u.role} disabled={self}
							onchange={(e) => act(() => send('PATCH', `/api/v1/admin/users/${u.id}`, { role: (e.target as HTMLSelectElement).value }), 'Rolle geändert')}>
							<option value="viewer">viewer</option><option value="operator">operator</option><option value="admin">admin</option>
						</select>
					</td>
					<td class="px-4 py-2">{u.totp_enabled ? '✓' : '–'}</td>
					<td class="px-4 py-2 text-xs">
						{#if u.disabled}<span class="text-red-500">deaktiviert</span>{:else if u.locked}<span class="text-amber-500">gesperrt (Fehlversuche)</span>{:else}aktiv{/if}
						{#if u.must_change_password}<div class="text-zinc-500">vorläufiges Passwort</div>{/if}
					</td>
					<td class="px-4 py-2 text-xs">{u.last_login_at ? dateTime(Date.parse(u.last_login_at) / 1000) : 'nie'}</td>
					<td class="px-4 py-2 text-right text-xs whitespace-nowrap">
						{#if !self}
							<button class="hover:text-accent" onclick={() => act(() => send('PATCH', `/api/v1/admin/users/${u.id}`, { disabled: !u.disabled }), u.disabled ? 'Aktiviert' : 'Deaktiviert')}>{u.disabled ? 'Aktivieren' : 'Deaktivieren'}</button>
							· <button class="hover:text-accent" onclick={() => resetPassword(u)}>Passwort</button>
							{#if u.totp_enabled}· <button class="hover:text-accent" onclick={() => resetTotp(u)}>TOTP</button>{/if}
							· <button class="text-red-500 hover:underline" onclick={() => remove(u)}>Löschen</button>
						{/if}
					</td>
				</tr>
			{/each}
		</tbody>
	</table>
</div>

<div class="mt-4 text-xs text-zinc-500">
	<strong>Rollen:</strong> Viewer sieht alles, ändert nichts. Operator darf Gäste starten/stoppen, Snapshots, Backups und Migrationen auslösen. Admin verwaltet zusätzlich Benutzer und sieht das gesamte Audit-Log.
</div>

{#if temp}
	<Modal title="Vorläufiges Passwort für {temp.username}" onclose={() => (temp = null)}>
		<p class="mb-3">Wird nur jetzt angezeigt. Gib es sicher weiter – beim ersten Login muss es geändert werden.</p>
		<code class="block rounded-lg bg-zinc-100 px-3 py-2 font-mono text-base select-all dark:bg-zinc-800">{temp.password}</code>
	</Modal>
{/if}

{#if confirm}
	<ConfirmDialog title={confirm.title} message={confirm.message} danger={confirm.danger} requireText={confirm.text ?? ''}
		confirmLabel="Ja" onconfirm={confirm.run} onclose={() => (confirm = null)} />
{/if}
