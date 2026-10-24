// Minimal toast notifications.

export interface Toast {
	id: number;
	kind: 'ok' | 'error' | 'info';
	text: string;
	action?: { label: string; run: () => void };
}

class Toasts {
	items = $state<Toast[]>([]);
	#next = 1;

	push(kind: Toast['kind'], text: string, action?: Toast['action'], ttl = 6000) {
		const id = this.#next++;
		this.items = [...this.items, { id, kind, text, action }];
		if (ttl) setTimeout(() => this.dismiss(id), ttl);
	}

	dismiss(id: number) {
		this.items = this.items.filter((t) => t.id !== id);
	}
}

export const toasts = new Toasts();

/** Global task-log viewer (rendered once in the layout). */
class TaskViewer {
	current = $state<{ cluster: string; upid: string } | null>(null);

	open(cluster: string, upid: string) {
		this.current = { cluster, upid };
	}

	close() {
		this.current = null;
	}
}

export const taskViewer = new TaskViewer();

/** Global live log viewer for dashboard jobs (upgrade, maintenance, reboot). */
class JobViewer {
	current = $state<number | null>(null);

	open(id: number) {
		this.current = id;
	}

	close() {
		this.current = null;
	}
}

export const jobViewer = new JobViewer();
