// Small fetch helper for the dashboard API.

export class ApiError extends Error {
	constructor(
		public status: number,
		message: string
	) {
		super(message);
	}
}

type Params = Record<string, string | number | boolean | null | undefined | string[]>;

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

export async function getJson<T>(path: string, params: Params = {}, signal?: AbortSignal): Promise<T> {
	const res = await fetch(url(path, params), { signal, headers: { Accept: 'application/json' } });
	if (!res.ok) {
		let detail = res.statusText;
		try {
			detail = (await res.json()).detail ?? detail;
		} catch {
			/* not JSON */
		}
		throw new ApiError(res.status, typeof detail === 'string' ? detail : JSON.stringify(detail));
	}
	return res.json() as Promise<T>;
}

export async function postJson<T>(path: string, body?: unknown): Promise<T> {
	const res = await fetch(path, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	if (!res.ok) throw new ApiError(res.status, res.statusText);
	return res.json() as Promise<T>;
}
