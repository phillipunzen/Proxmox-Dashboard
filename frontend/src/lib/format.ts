// Display helpers (German locale).

const UNITS = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];

export function bytes(value: number | null | undefined, digits = 1): string {
	if (value == null || !Number.isFinite(value)) return '–';
	let v = Math.abs(value);
	let i = 0;
	while (v >= 1024 && i < UNITS.length - 1) {
		v /= 1024;
		i++;
	}
	return `${(Math.sign(value) * v).toLocaleString('de-DE', { maximumFractionDigits: i ? digits : 0 })} ${UNITS[i]}`;
}

export function rate(value: number | null | undefined): string {
	return value == null ? '–' : `${bytes(value)}/s`;
}

export function pct(value: number | null | undefined, digits = 0): string {
	if (value == null || !Number.isFinite(value)) return '–';
	return `${(value * 100).toLocaleString('de-DE', { maximumFractionDigits: digits, minimumFractionDigits: digits })} %`;
}

export function ratio(used: number | null | undefined, total: number | null | undefined): number {
	return used != null && total ? used / total : 0;
}

export function num(value: number | null | undefined, digits = 2): string {
	return value == null ? '–' : value.toLocaleString('de-DE', { maximumFractionDigits: digits });
}

export function uptime(seconds: number | null | undefined): string {
	if (!seconds) return '–';
	const d = Math.floor(seconds / 86400);
	const h = Math.floor((seconds % 86400) / 3600);
	const m = Math.floor((seconds % 3600) / 60);
	if (d) return `${d} T ${h} h`;
	if (h) return `${h} h ${m} min`;
	return `${m} min`;
}

export function timeAgo(iso: string | null, now = Date.now()): string {
	if (!iso) return 'nie';
	const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
	if (s < 60) return `vor ${s} s`;
	if (s < 3600) return `vor ${Math.floor(s / 60)} min`;
	return `vor ${Math.floor(s / 3600)} h`;
}

/** Tailwind colour class for a utilisation level. */
export function levelColor(value: number, warn = 0.75, crit = 0.9): string {
	if (value >= crit) return 'bg-red-500';
	if (value >= warn) return 'bg-amber-500';
	return 'bg-emerald-500';
}
