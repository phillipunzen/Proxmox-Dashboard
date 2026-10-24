// Live state fed by the backend's SSE stream (/api/v1/stream).
//
// The backend pushes the complete snapshot of all clusters after every poll. We also keep a
// short rolling history in the browser for the live charts; long-term history comes from the
// time-series database in phase 2.

import type { ClusterSnapshot, Meta } from './types';

export interface Point {
	t: number; // epoch ms
	cpu: number;
	mem: number; // ratio 0..1
	netin?: number;
	netout?: number;
}

const HISTORY_POINTS = 360; // 1 h at a 10 s poll interval

class LiveState {
	clusters = $state<ClusterSnapshot[]>([]);
	meta = $state<Meta | null>(null);
	connected = $state(false);
	lastMessageAt = $state<number | null>(null);
	/** Cluster filter for all views; null = all clusters. */
	selected = $state<string | null>(null);
	/** Rolling history keyed by "cluster:<name>" and "node:<cluster>/<node>". */
	history = $state.raw<Record<string, Point[]>>({});

	#source: EventSource | null = null;

	get visible(): ClusterSnapshot[] {
		return this.selected ? this.clusters.filter((c) => c.name === this.selected) : this.clusters;
	}

	async start() {
		if (this.#source) return;
		try {
			const res = await fetch('/api/v1/meta');
			if (res.ok) this.meta = await res.json();
		} catch {
			/* backend not reachable yet - the stream will retry */
		}
		const source = new EventSource('/api/v1/stream');
		source.onopen = () => (this.connected = true);
		source.onerror = () => (this.connected = false); // EventSource reconnects by itself
		source.addEventListener('snapshot', (ev) => {
			const payload = JSON.parse((ev as MessageEvent).data) as { clusters: ClusterSnapshot[] };
			this.connected = true;
			this.lastMessageAt = Date.now();
			this.clusters = payload.clusters;
			this.#record(payload.clusters);
		});
		this.#source = source;
	}

	stop() {
		this.#source?.close();
		this.#source = null;
	}

	#record(clusters: ClusterSnapshot[]) {
		const next = { ...this.history };
		const push = (key: string, p: Point) => {
			const arr = next[key] ?? [];
			// Skip duplicates (same snapshot timestamp delivered twice, e.g. after reconnect).
			if (arr.length && arr[arr.length - 1].t >= p.t) return;
			next[key] = [...arr.slice(-(HISTORY_POINTS - 1)), p];
		};
		for (const c of clusters) {
			if (!c.updated_at || !c.reachable) continue;
			const t = Date.parse(c.updated_at);
			const tot = c.totals;
			// Right after (re)start the backend has no previous counters, so rates are null.
			const hasRates = c.guests.some((g) => g.netin_bps != null);
			push(`cluster:${c.name}`, {
				t,
				cpu: tot.cpu,
				mem: tot.maxmem ? tot.mem / tot.maxmem : 0,
				netin: hasRates ? tot.netin_bps : undefined,
				netout: hasRates ? tot.netout_bps : undefined
			});
			for (const n of c.nodes) {
				if (n.status !== 'online') continue;
				push(`node:${c.name}/${n.node}`, { t, cpu: n.cpu, mem: n.maxmem ? n.mem / n.maxmem : 0 });
			}
		}
		this.history = next;
	}
}

export const live = new LiveState();
