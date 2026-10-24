// Session state of the logged-in user.
import { getJson, send, setCsrfToken } from './api';

export type Role = 'viewer' | 'operator' | 'admin';
export type Pending = 'totp' | 'totp_setup' | 'password_change' | null;
const LEVEL: Record<Role, number> = { viewer: 0, operator: 1, admin: 2 };

export interface Me {
	username: string;
	role: Role;
	auth_source: 'local' | 'oidc';
	totp_enabled: boolean;
	totp_required: boolean;
	must_change_password: boolean;
	pending: Pending;
	csrf_token: string;
}

export interface Capability {
	cluster: string;
	enabled: boolean;
	reason: string | null;
	dry_run: boolean;
}

class AuthState {
	me = $state<Me | null>(null);
	checked = $state(false);
	capabilities = $state<Record<string, Capability>>({});

	get ready(): boolean {
		return !!this.me && !this.me.pending;
	}

	can(role: Role): boolean {
		return !!this.me && !this.me.pending && LEVEL[this.me.role] >= LEVEL[role];
	}

	/** Operator role *and* actions enabled for that cluster (not READ_ONLY, token present). */
	canControl(cluster: string): boolean {
		return this.can('operator') && !!this.capabilities[cluster]?.enabled;
	}

	set(me: Me | null) {
		this.me = me;
		setCsrfToken(me?.csrf_token ?? '');
	}

	async load(): Promise<Me | null> {
		try {
			this.set(await getJson<Me>('/api/v1/auth/me'));
		} catch {
			this.set(null);
		}
		this.checked = true;
		if (this.ready) await this.loadCapabilities();
		return this.me;
	}

	async loadCapabilities() {
		try {
			const caps = await getJson<Capability[]>('/api/v1/actions/capabilities');
			this.capabilities = Object.fromEntries(caps.map((c) => [c.cluster, c]));
		} catch {
			this.capabilities = {};
		}
	}

	async logout() {
		try {
			await send('POST', '/api/v1/auth/logout');
		} finally {
			this.set(null);
		}
	}
}

export const auth = new AuthState();
