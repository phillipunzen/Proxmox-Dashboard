// Mirrors backend/app/collector/models.py (API v1). Keep in sync.

export type Severity = 'info' | 'warning' | 'critical';
export type GuestType = 'qemu' | 'lxc';

export interface ClusterWarning {
	severity: Severity;
	target: string;
	message: string;
}

export interface NodeView {
	cluster: string;
	node: string;
	status: 'online' | 'offline' | 'unknown';
	ip: string | null;
	cpu: number;
	maxcpu: number;
	mem: number;
	maxmem: number;
	swap_used: number | null;
	swap_total: number | null;
	rootfs_used: number | null;
	rootfs_total: number | null;
	iowait: number | null;
	loadavg: number[] | null;
	uptime: number;
	pve_version: string | null;
	kernel: string | null;
	cpu_model: string | null;
	cpu_sockets: number | null;
	cpu_cores: number | null;
	guests_running: number;
	guests_total: number;
	vcpus_allocated: number;
	mem_allocated: number;
}

export interface GuestView {
	cluster: string;
	vmid: number;
	name: string;
	type: GuestType;
	node: string;
	status: string;
	template: boolean;
	cpu: number;
	cpu_host: number;
	maxcpu: number;
	mem: number;
	maxmem: number;
	disk: number;
	maxdisk: number;
	diskread_bps: number | null;
	diskwrite_bps: number | null;
	netin_bps: number | null;
	netout_bps: number | null;
	uptime: number;
	tags: string[];
	pool: string | null;
	lock: string | null;
	console_url: string | null;
}

export interface StorageView {
	cluster: string;
	node: string;
	storage: string;
	type: string;
	status: string;
	shared: boolean;
	used: number;
	total: number;
	content: string[];
}

export interface ClusterTotals {
	nodes_online: number;
	nodes_total: number;
	cpu: number;
	cpus: number;
	mem: number;
	maxmem: number;
	swap_used: number;
	swap_total: number;
	storage_used: number;
	storage_total: number;
	netin_bps: number;
	netout_bps: number;
	diskread_bps: number;
	diskwrite_bps: number;
	iowait: number | null;
	load1: number | null;
	vms_running: number;
	vms_stopped: number;
	lxc_running: number;
	lxc_stopped: number;
	templates: number;
	vcpus_allocated: number;
	mem_allocated: number;
}

export interface ClusterSnapshot {
	name: string;
	is_cluster: boolean;
	reachable: boolean;
	quorate: boolean | null;
	api_url: string | null;
	ui_url: string | null;
	pve_version: string | null;
	updated_at: string | null;
	last_error: string | null;
	totals: ClusterTotals;
	nodes: NodeView[];
	guests: GuestView[];
	storages: StorageView[];
	warnings: ClusterWarning[];
	ha_enabled: boolean | null;
	ceph_health: string | null;
}

export interface Meta {
	version: string;
	mock: boolean;
	read_only: boolean;
	poll_interval: number;
	clusters: string[];
}
