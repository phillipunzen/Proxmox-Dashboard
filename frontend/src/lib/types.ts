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
	dry_run: boolean;
	poll_interval: number;
	history: boolean;
	clusters: string[];
}

// --- Phase 2: history, analysis, inventory -------------------------------------------------

export type RangeKey = '1h' | '6h' | '24h' | '7d' | '30d';
export type AnalysisMetric = 'cpu' | 'mem' | 'diskread' | 'diskwrite' | 'netin' | 'netout';
export type Pt = [number, number]; // [unix seconds, value]

export interface SeriesSet {
	start: number;
	end: number;
	step: number;
	series: Record<string, Pt[]>;
}

export interface GuestSeries {
	vmid: number;
	name: string;
	type: string;
	cluster: string | null;
	avg: number;
	max: number;
	points: Pt[];
}

export interface Contribution {
	metric: AnalysisMetric;
	unit: 'cores' | 'bytes' | 'bps';
	start: number;
	end: number;
	step: number;
	total: Pt[];
	capacity: Pt[] | null;
	guests: GuestSeries[];
	other: Pt[];
	other_count: number;
}

export interface RankedGuest {
	cluster: string;
	vmid: number;
	name: string;
	type: string;
	node: string | null;
	value: number;
	share_of_total: number | null;
	share_of_capacity: number | null;
}

export interface Ranking {
	metric: AnalysisMetric;
	unit: 'cores' | 'bytes' | 'bps';
	at: number;
	window: number;
	total: number | null;
	capacity: number | null;
	guests: RankedGuest[];
}

export interface TopGuest {
	cluster: string;
	vmid: number;
	name: string;
	type: string;
	node: string | null;
	avg: number;
	p95: number;
	max: number;
}

export interface NodeAllocation {
	cluster: string;
	node: string;
	online: boolean;
	threads: number;
	mem_total: number;
	vcpus_running: number;
	vcpus_configured: number;
	mem_running: number;
	mem_configured: number;
	cpu_ratio_running: number | null;
	cpu_ratio_configured: number | null;
	mem_ratio_running: number | null;
	mem_ratio_configured: number | null;
}

export interface Disk {
	devpath: string;
	model: string | null;
	vendor: string | null;
	serial: string | null;
	wwn: string | null;
	size: number;
	type: string;
	rpm: number | null;
	health: string | null;
	wearout_used: number | null;
	used_by: string | null;
}

export interface NetIface {
	iface: string;
	type: string;
	active: boolean;
	autostart: boolean;
	method: string | null;
	cidr: string | null;
	gateway: string | null;
	cidr6: string | null;
	bridge_ports: string[];
	bridge_vlan_aware: boolean;
	slaves: string[];
	bond_mode: string | null;
	vlan_id: number | null;
	vlan_raw_device: string | null;
	mtu: number | null;
	comments: string | null;
}

export interface PciDevice {
	id: string;
	class_code: string | null;
	class_name: string | null;
	vendor_name: string | null;
	device_name: string | null;
	subsystem: string | null;
	iommu_group: number | null;
	mdev: boolean;
	assigned_to: { cluster: string; vmid: number; name: string; key: string }[];
}

export interface NodeHardware {
	cluster: string;
	node: string;
	online: boolean;
	cpu_model: string | null;
	cpu_sockets: number | null;
	cpu_cores: number | null;
	cpu_threads: number | null;
	cpu_mhz: number | null;
	mem_total: number | null;
	kernel: string | null;
	pve_version: string | null;
	boot_mode: string | null;
	secure_boot: boolean | null;
	mainboard: string | null;
	disks: Disk[];
	network: NetIface[];
	pci: PciDevice[];
	collected_at: string | null;
	errors: string[];
}

export interface GuestDisk {
	key: string;
	storage: string | null;
	volume: string;
	size: number | null;
	media: string | null;
	mountpoint: string | null;
	options: Record<string, string>;
}

export interface GuestNic {
	key: string;
	model: string | null;
	mac: string | null;
	bridge: string | null;
	vlan: number | null;
	firewall: boolean;
	rate_mbps: number | null;
	name: string | null;
	ip_config: string | null;
}

export interface GuestInventory {
	cluster: string;
	vmid: number;
	name: string;
	type: GuestType;
	node: string;
	status: string;
	template: boolean;
	cores: number | null;
	sockets: number | null;
	vcpus: number | null;
	cpu_type: string | null;
	cpu_limit: number | null;
	memory: number | null;
	balloon_min: number | null;
	swap: number | null;
	disks: GuestDisk[];
	nics: GuestNic[];
	hostpci: { key: string; devices: string[]; mapping: string | null; options: Record<string, string> }[];
	usb: string[];
	bios: string | null;
	machine: string | null;
	ostype: string | null;
	arch: string | null;
	unprivileged: boolean | null;
	agent_enabled: boolean | null;
	agent_running: boolean | null;
	os_name: string | null;
	ip_addresses: string[];
	onboot: boolean;
	protection: boolean;
	tags: string[];
	notes: string | null;
	collected_at: string | null;
	error: string | null;
}

export interface ClusterInventory {
	cluster: string;
	nodes: NodeHardware[];
	guests: GuestInventory[];
	collected_at: string | null;
	duration_s: number | null;
}

// --- Phase 4: node management, jobs, alerts ------------------------------------------------

export interface NodeStatus {
	cluster: string;
	node: string;
	online: boolean;
	maintenance: boolean;
	maintenance_since: string | null;
	maintenance_by: string | null;
	evacuated: { vmid: number; name: string; type: string; target: string | null }[];
	updates: number | null;
	updates_checked_at: string | null;
	updates_error: string | null;
	reboot_required: boolean | null;
	temperature: number | null;
	ssh_host: string | null;
	ssh_trusted: boolean;
	running_job: number | null;
	can_manage: boolean;
	manage_reason: string | null;
}

export interface Package {
	package: string;
	old_version: string | null;
	new_version: string;
	title: string | null;
	origin: string | null;
	section: string | null;
	priority: string | null;
}

export interface UpdateInfo {
	cluster: string;
	node: string;
	packages: Package[];
	checked_at: string | null;
	error: string | null;
	reboot_required: boolean | null;
}

export interface Job {
	id: number;
	kind: string;
	cluster: string;
	node: string | null;
	status: 'running' | 'ok' | 'error' | 'cancelled';
	started_by: string;
	started_at: string;
	finished_at: string | null;
	summary: string | null;
	params: Record<string, unknown>;
}

export interface AlertEvent {
	id: number;
	rule_id: number | null;
	rule_name: string;
	target: string;
	severity: Severity;
	state: 'firing' | 'resolved';
	value: number | null;
	message: string;
	started_at: string;
	resolved_at: string | null;
	acknowledged_by: string | null;
}

export interface AlertRule {
	id?: number;
	name: string;
	enabled: boolean;
	metric: string;
	operator: '>' | '>=' | '<' | '<=';
	threshold: number;
	for_seconds: number;
	severity: Severity;
	selector: Record<string, string | number>;
	channel_ids: number[];
	repeat_minutes: number;
}

export interface MetricInfo {
	key: string;
	label: string;
	unit: 'ratio' | 'celsius' | 'count' | 'bool' | 'percent';
	kind: string;
	default_operator: string;
	default_threshold: number;
}

export interface Channel {
	id?: number;
	name: string;
	type: 'webhook' | 'ntfy' | 'gotify' | 'email' | 'homeassistant';
	config: Record<string, string | number | string[]>;
	enabled: boolean;
	min_severity: Severity;
	send_resolved: boolean;
}
