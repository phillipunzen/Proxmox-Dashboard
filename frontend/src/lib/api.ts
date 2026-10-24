// Small fetch helper for the dashboard API.
//
// Write requests carry the CSRF token of the session (X-CSRF-Token). A 401 anywhere means the
// session is gone: the registered handler sends the user back to the login page.

export class ApiError extends Error {
	constructor(
		public status: number,
		message: string
	) {
		super(message);
	}
}

type Params = Record<string, string | number | boolean | null | undefined | string[]>;

let csrfToken = '';
let onUnauthorized: (() => void) | null = null;

export function setCsrfToken(token: string) {
	csrfToken = token;
}

export function setUnauthorizedHandler(fn: () => void) {
	onUnauthorized = fn;
}

export function url(path: string, params: Params = {}): string {
	const qs = new URLSearchParams();
	for (const [k, v] of Object.entries(params)) {
		if (v == null || v === '') continue;
		if (Array.isArray(v)) v.forEach((x) => qs.append(k, x));
		else qs.set(k, String(v));
	}
	const q = qs.toString();
	return q ? `${path}?${q}` : path;
}

async function handle<T>(res: Response, path: string): Promise<T> {
	if (res.status === 401 && !path.startsWith('/api/v1/auth/')) onUnauthorized?.();
	if (!res.ok) {
		let detail: unknown = res.statusText;
		try {
			detail = (await res.json()).detail ?? detail;
		} catch {
			/* not JSON */
		}
		// FastAPI validation errors come as a list.
		const msg = Array.isArray(detail)
			? detail.map((d) => d.msg ?? JSON.stringify(d)).join('; ')
			: typeof detail === 'string'
				? detail
				: JSON.stringify(detail);
		throw new ApiError(res.status, msg);
	}
	if (res.status === 204) return undefined as T;
	return res.json() as Promise<T>;
}

export async function getJson<T>(path: string, params: Params = {}, signal?: AbortSignal): Promise<T> {
	const res = await fetch(url(path, params), { signal, headers: { Accept: 'application/json' } });
	return handle<T>(res, path);
}

export async function send<T>(
	method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
	path: string,
	body?: unknown,
	params: Params = {}
): Promise<T> {
	const res = await fetch(url(path, params), {
		method,
		headers: {
			Accept: 'application/json',
			'X-CSRF-Token': csrfToken,
			...(body === undefined ? {} : { 'Content-Type': 'application/json' })
		},
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	return handle<T>(res, path);
}

export const postJson = <T>(path: string, body?: unknown) => send<T>('POST', path, body);
