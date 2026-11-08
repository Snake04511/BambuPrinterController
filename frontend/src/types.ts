// Shapes of the backend snapshot (backend/app/poller.py snapshot()).

export interface Tray {
  id: number
  type: string
  sub_brands: string
  info_idx: string
  has_filament: boolean
  color: string // '#RRGGBB' or ''
  remain: number // %, -1 unknown
  state: number
  temp_min: number
  temp_max: number
  k: number
  rfid: boolean
}

export interface AmsUnit {
  id: number
  humidity: number
  temp: number | null
  is_ht: boolean
  trays: (Tray | null)[]
}

export interface Hms {
  severity: number
  text: string
}

export interface Status {
  connected: boolean
  state: string
  job: string
  progress: number
  remaining_min: number
  layer: number
  total_layers: number
  nozzle: number
  nozzle_target: number
  bed: number
  bed_target: number
  has_chamber: boolean
  chamber: number
  chamber_target: number
  chamber_heater: boolean
  light: boolean
  timelapse: boolean
  speed_level: number
  fan_part: number
  fan_aux: number
  fan_chamber: number
  wifi_signal: string
  door_open: boolean
  awaiting_plate_clear: boolean
  tray_now: number
  ams: AmsUnit[]
  ext: Tray[]
  hms: Hms[]
  hms_severity: number
}

export interface Spool {
  id: number
  material: string
  subtype: string
  brand: string
  color_name: string
  color: string
  label_weight: number
  weight_used: number
  temp_min: number
  temp_max: number
}

export interface Assignment {
  ams_id: number
  tray_id: number
  spool: Spool
}

export interface MaintItem {
  id: number
  name: string
  due: boolean
  warning: boolean
  by_days: boolean
  until: number
}

export interface Notif {
  id: number
  title: string
  message: string
  time: string
  success: boolean
}

export interface Obico {
  enabled: boolean
  running: boolean
  low: number
  high: number
  sensitivity: string
  action: string
  error: string
  has_printer: boolean
  cls: string
  score: number
  frames: number
}

export interface CloudInfo {
  logged_in: boolean
  email: string
  china: boolean
  expiry: string
  expiry_warn: boolean
}

export interface Settings {
  printer_name: string
  chamber_control: boolean
  file_previews: boolean
  sd_while_printing: boolean
  print_timelapse: boolean
  print_bed_level: boolean
  print_flow_cali: boolean
  print_speed_l1: number
  light_off_on_end: boolean
  light_on_print: boolean
  hide_external: boolean
  record_timelapse: boolean
  light_with_obico: boolean
  timelapse_keep_count: number
  timelapse_keep_gb: number
  data_limit_gb: number
  file_cache_gb: number
  backup_enabled: boolean
  currency: string
  summary_mode: string
  dry_humidity: number
  dry_hours: number
  dry_materials: string
  summary_hour: number
  power_watts: number
  kwh_price: number
  backup_keep: number
  notif_seen_id: number
  obico_enabled: boolean
  obico_ml_url: string
  obico_sensitivity: string
  obico_action: string
  obico_poll_interval: number
  obico_frame_base_url: string
  printer_ip: string
  printer_serial: string
  notify_discord_enabled: boolean
  notify_ntfy_enabled: boolean
  ntfy_server: string
  ntfy_topic: string
  notify_app_url: string
  [notifyOn: `notify_on_${string}`]: boolean
}

export interface Notice {
  id: number
  ok: boolean
  text: string
  t: number
}

export interface PrintEnd {
  failed: boolean
  job: string
  duration_min: number
  layers: number
  layer_reached: number
  avg_layer_sec: number
  ended_at: number
  filament?: EndFilament[]
  filament_state?: 'loading' | 'done' | 'none'
  archive_sec?: number
  archive_id?: number
}

export interface EndFilament {
  slot: number
  type: string
  color: string
  grams: number
  meters: number
  where: string // tray label from the cloud fallback, '' otherwise
}

export interface LowTray {
  id: number
  label: string
  type: string
  sub_brands: string
  color: string
  remain: number
  spool_left: number | null
  active: boolean
}

export interface Runout {
  tray: number
  left: number
  total: number
  at_pct: number
  minutes: number
  short: boolean
  text: string
}

export interface Alerts {
  low: LowTray[]
  runout: Runout | null
  humid: number // worst humidity of a unit too humid for 10 min, -1 none
}

export interface PrintObject {
  id: number
  name: string
  x: number
  y: number
  skipped: boolean
}

export interface PrintObjects {
  job: string
  current: boolean
  loaded: boolean
  loading: boolean
  error: string
  items: PrintObject[]
  bbox: [number, number, number, number] | null
  has_pick: boolean
  version: number
}

export type Link = 'not_configured' | 'offline' | 'online'

export interface Snapshot {
  version: number
  link: Link
  plate_seq?: import('./plateseq').PlateSeq | null
  link_text: string
  printer: { id: number; name: string; model: string } | null
  status: Status | null
  assignments: Assignment[]
  maintenance: { due_count: number; warning_count: number; items: MaintItem[] } | null
  notifications: { items: Notif[]; newest_id: number } | null
  notif_seen_id: number
  obico: Obico | null
  cover_job: string
  cover_version: number
  busy: boolean
  layer_stats: { avg_sec: number; last_sec: number }
  l1_speed: number
  end_light_remaining: number
  print_starts: number
  print_ends: number
  print_pauses: number
  last_end: PrintEnd | null
  notices: Notice[]
  cloud: CloudInfo
  settings: Settings
  start: { phase: 'idle' | 'sending' | 'waiting' | 'started' | 'failed'; file: string; message: string; t?: number }
  sd_version: number
  cloud_tasks_loaded: boolean
  timelapse_id: string
  timelapse_version: number
  usage_pending: number
  usage_version: number
  objects_count: number // -1 unknown
  objects_version: number
  alerts: Alerts
  filament_use: { total: number; progress: number | null; by_tray: Record<string, number>; need_by_tray?: Record<string, number> } | null
}
