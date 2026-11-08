// SETTINGS (panel src/ui/pages/SettingsPage.cpp + the browser setup page src/net/WebConfig.cpp).
import { useEffect, useState, type ReactNode } from 'react'
import { api, cmd, type ApiError } from '../api'
import type { CloudInfo, Settings as S, Snapshot } from '../types'
import { Modal, Toggle, useDialogs } from '../ui'
import { CameraRestart } from '../media'

type SecretKey = 'printer_access_code' | 'discord_webhook_url' | 'ntfy_token' | 'obico_ml_token' | 'panel_api_key'
type Patch = Partial<S> & Partial<Record<SecretKey, string | null>>

interface SettingsView {
  settings: S
  secrets: { printer_access_code: boolean; discord_webhook_url: boolean; ntfy_token: boolean; obico_ml_token: boolean; panel_api_key: boolean }
  notify_events: { key: string; label: string }[]
  mqtt: { link: string; connected: boolean; error: string; last_message_s: number }
  ftp: { busy: boolean; current: string | null; waiting: number; last_error: string; cooloff_s: number }
  camera: { direct: boolean; connected?: boolean; fps?: number; last_error?: string; auth_suspect?: boolean; ipcam_record?: boolean | null }
  cloud: CloudInfo
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="card p-4 md:p-5">
      <h2 className="section-title">{title}</h2>
      <div className="flex flex-col gap-4">{children}</div>
    </section>
  )
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-4">
      <div className="min-w-0 flex-1">
        <div className="font-semibold">{label}</div>
        {hint && <div className="text-sm text-muted">{hint}</div>}
      </div>
      {children}
    </div>
  )
}

export default function Settings({ snap, onLogout }: { snap: Snapshot | null; onLogout: () => void }) {
  const dlg = useDialogs()
  const [view, setView] = useState<SettingsView | null>(null)
  const reload = () => api<SettingsView>('/api/settings').then(setView).catch(() => {})
  useEffect(() => {
    reload()
  }, [snap?.cloud.logged_in, snap?.link])

  const patch = async (changes: Patch) => {
    try {
      setView(await api<SettingsView>('/api/settings', { method: 'PATCH', body: changes }))
      return true
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
      return false
    }
  }

  if (!view) return <div className="p-6 text-muted">Loading...</div>
  const s = view.settings

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 p-3 md:p-4">
      <PrinterLanSection view={view} patch={patch} />
      <CloudSection cloud={snap?.cloud ?? view.cloud} />
      <DryingSection view={view} patch={patch} />
      <MaintenanceSection />
      <PanelSection view={view} patch={patch} reload={reload} />
      <ObicoSection view={view} patch={patch} />
      <NotifySection view={view} patch={patch} />

      <Section title="Features">
        <Row label="Chamber temperature control" hint="Show and allow setting the chamber temperature">
          <Toggle checked={s.chamber_control} onChange={(v) => patch({ chamber_control: v })} />
        </Row>
        <Row label="File previews" hint="Preview images of SD card files (downloaded and cached in the background)">
          <Toggle checked={s.file_previews} onChange={(v) => patch({ file_previews: v })} />
        </Row>
        <Row label="Read SD files while printing" hint="Background downloads from the printer during a print too (slower FTP)">
          <Toggle checked={s.sd_while_printing} onChange={(v) => patch({ sd_while_printing: v })} />
        </Row>
        <Row label="Light off 2 min after a print ends">
          <Toggle checked={s.light_off_on_end} onChange={(v) => patch({ light_off_on_end: v })} />
        </Row>
        <Row label="Light on during every print" hint="Day and night (camera / Obico detection)">
          <Toggle checked={s.light_on_print} onChange={(v) => patch({ light_on_print: v })} />
        </Row>
        <Row label="Light on while Obico watches" hint="With Obico detection on: light on for the whole print, switched back on if turned off">
          <Toggle checked={s.light_with_obico} onChange={(v) => patch({ light_with_obico: v })} />
        </Row>
        <Row label="Record a timelapse of every print" hint="Camera frame per Obico analysis (~10 s), with its score; kept on the server, oldest go first (3 GB)">
          <Toggle checked={s.record_timelapse} onChange={(v) => patch({ record_timelapse: v })} />
        </Row>
      </Section>

      <Section title="Printer">
        <div className="flex flex-wrap gap-2">
          <button className="btn" disabled={snap?.link !== 'online'} onClick={() => cmd('clear_hms')}>
            Clear printer errors
          </button>
          <button className="btn" disabled={snap?.link !== 'online'} onClick={() => cmd('refresh')}>
            Refresh status now
          </button>
        </div>
      </Section>

      <CostSection view={view} patch={patch} />
      <StorageSection view={view} patch={patch} />
      <AccountSection onLogout={onLogout} />

      <Section title="Info">
        <div className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-sm">
          <span className="text-muted">Printer</span>
          <span>{snap?.printer ? `${snap.printer.name} ${snap.printer.model}` : '-'}</span>
          <span className="text-muted">Connection</span>
          <span>{snap?.link === 'online' ? 'Connected' : snap?.link_text || snap?.link || '-'}</span>
        </div>
      </Section>
    </div>
  )
}

// ---------------------------------------------------------------- secrets / text fields

/** Password-style field for a secret: never shows the value, only whether one is stored. */
function SecretField({ label, isSet, placeholder, hint, onSave }: { label: string; isSet: boolean; placeholder: string; hint?: string; onSave: (v: string | null) => Promise<boolean> }) {
  const [v, setV] = useState('')
  return (
    <div>
      <label className="label">{label}</label>
      <div className="flex gap-2">
        <input
          className="input min-w-0 flex-1"
          type="password"
          autoComplete="off"
          value={v}
          onChange={(e) => setV(e.target.value)}
          placeholder={isSet ? 'Stored - type to replace' : placeholder}
        />
        <button className={v ? 'btn-accent' : 'btn'} disabled={!v} onClick={async () => (await onSave(v)) && setV('')}>
          Save
        </button>
        {isSet && (
          <button className="btn" onClick={() => onSave(null)} title="Remove the stored value">
            Clear
          </button>
        )}
      </div>
      {hint && <p className="mt-1 text-sm text-muted">{hint}</p>}
    </div>
  )
}

function TextField({ label, value, placeholder, hint, onSave }: { label: string; value: string; placeholder?: string; hint?: string; onSave: (v: string) => void }) {
  const [v, setV] = useState(value)
  useEffect(() => setV(value), [value])
  return (
    <div>
      <label className="label">{label}</label>
      <input className="input" value={v} placeholder={placeholder} onChange={(e) => setV(e.target.value)} onBlur={() => v !== value && onSave(v)} />
      {hint && <p className="mt-1 text-sm text-muted">{hint}</p>}
    </div>
  )
}

// ---------------------------------------------------------------- spool drying reminder

function DryingSection({ view, patch }: { view: SettingsView; patch: (c: Patch) => Promise<boolean> }) {
  return (
    <Section title="Spool drying reminder">
      <p className="-mt-2 text-sm text-muted">
        A moisture-sensitive spool in an AMS above the humidity limit collects "humid hours"; past the limit you get a push and the Spools
        page shows "needs drying" until you press Dried.
      </p>
      <Row label="Humid above (%)">
        <input
          key={`dh-${view.settings.dry_humidity}`}
          className="input h-9 w-20"
          type="number"
          min={5}
          max={95}
          defaultValue={view.settings.dry_humidity}
          onBlur={(e) => Number(e.target.value) !== view.settings.dry_humidity && patch({ dry_humidity: Math.round(Number(e.target.value) || 40) })}
        />
      </Row>
      <Row label="Remind after (humid hours)">
        <input
          key={`dhr-${view.settings.dry_hours}`}
          className="input h-9 w-20"
          type="number"
          min={1}
          max={2000}
          defaultValue={view.settings.dry_hours}
          onBlur={(e) => Number(e.target.value) !== view.settings.dry_hours && patch({ dry_hours: Math.round(Number(e.target.value) || 24) })}
        />
      </Row>
      <TextField label="Materials" value={view.settings.dry_materials} placeholder="PETG,TPU,PA,..." onSave={(v) => patch({ dry_materials: v })} />
    </Section>
  )
}

// ---------------------------------------------------------------- costs

function CostSection({ view, patch }: { view: SettingsView; patch: (c: Patch) => Promise<boolean> }) {
  const s = view.settings
  const numField = (k: 'power_watts' | 'kwh_price', step: number) => (
    <input
      key={`${k}-${s[k]}`}
      className="input h-9 w-28 tabular-nums"
      type="number"
      min={0}
      step={step}
      defaultValue={s[k]}
      onBlur={(e) => {
        const v = Math.max(0, Number(e.target.value) || 0)
        if (v !== s[k]) patch({ [k]: k === 'power_watts' ? Math.round(v) : v } as unknown as Patch)
      }}
    />
  )
  return (
    <Section title="Costs">
      <p className="-mt-2 text-sm text-muted">
        Every print gets a cost in History and Stats: the grams used x the spool's price per kg (set it on the Spools page) plus the
        electricity for the print time.
      </p>
      <Row label="Currency">
        <input
          key={`cur-${s.currency}`}
          className="input h-9 w-28"
          defaultValue={s.currency}
          maxLength={8}
          onBlur={(e) => e.target.value.trim() !== s.currency && patch({ currency: e.target.value.trim() || 'Ft' })}
        />
      </Row>
      <Row label="Printer power (W)" hint="Average while printing; a P1S with a heated bed is about 100-150 W">
        {numField('power_watts', 10)}
      </Row>
      <Row label={`Electricity price (${s.currency} / kWh)`}>{numField('kwh_price', 1)}</Row>
    </Section>
  )
}

// ---------------------------------------------------------------- storage

function StorageSection({ view, patch }: { view: SettingsView; patch: (c: Patch) => Promise<boolean> }) {
  const s = view.settings
  const [st, setSt] = useState<{ data_bytes: number; timelapse_bytes: number; timelapse_count: number; disk_free_bytes: number } | null>(null)
  const load = () => api<typeof st>('/api/storage').then(setSt).catch(() => {})
  useEffect(() => {
    load()
  }, [])
  const gb = (b: number) => (b >= 1024 ** 3 ? `${(b / 1024 ** 3).toFixed(2)} GB` : `${Math.round(b / 1024 ** 2)} MB`)
  const num = (k: 'timelapse_keep_count' | 'timelapse_keep_gb' | 'data_limit_gb' | 'file_cache_gb', min: number, max: number) => (
    <input
      key={`${k}-${s[k]}`}
      className="input h-9 w-24 tabular-nums"
      type="number"
      min={min}
      max={max}
      defaultValue={s[k]}
      onBlur={async (e) => {
        const v = Math.max(min, Math.min(max, Math.round(Number(e.target.value) || min)))
        if (v !== s[k] && (await patch({ [k]: v } as unknown as Patch))) setSt(await api('/api/storage/prune', { body: {} }))
      }}
    />
  )
  const pct = st ? Math.min(100, (100 * st.data_bytes) / (s.data_limit_gb * 1024 ** 3)) : 0
  return (
    <Section title="Storage">
      {st && (
        <div>
          <div className="mb-1 flex items-baseline justify-between text-sm">
            <span>
              <b>{gb(st.data_bytes)}</b> <span className="text-muted">of {s.data_limit_gb} GB used</span>
            </span>
            <span className="text-xs text-muted">
              timelapses {gb(st.timelapse_bytes)} ({st.timelapse_count}) · {gb(st.disk_free_bytes)} free on the server
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-card2">
            <div className="h-full rounded-full bg-accent" style={{ width: `${Math.max(1, pct)}%` }} />
          </div>
        </div>
      )}
      <Row label="Timelapses kept" hint="The oldest recording goes first">
        {num('timelapse_keep_count', 1, 1000)}
      </Row>
      <Row label="Timelapses at most (GB)" hint="All recordings and their MP4 videos together">
        {num('timelapse_keep_gb', 1, 1000)}
      </Row>
      <Row label="Printer file copies at most (GB)" hint="3MF files read from the SD card are kept, so previews, objects, downloads and the 3D view never fetch them again; the least used go first">
        {num('file_cache_gb', 1, 1000)}
      </Row>
      <Row label="App data at most (GB)" hint="Everything the app stores; old timelapses are removed to stay under it">
        {num('data_limit_gb', 1, 2000)}
      </Row>
      <div className="border-t border-border" />
      <BackupRows view={view} patch={patch} />
    </Section>
  )
}

function BackupRows({ view, patch }: { view: SettingsView; patch: (c: Patch) => Promise<boolean> }) {
  const dlg = useDialogs()
  const s = view.settings
  const [b, setB] = useState<{ available: boolean; dir: string; last: { name: string; time: number; ok: boolean } | null; items: { name: string; size: number; time: number }[] } | null>(null)
  const [busy, setBusy] = useState(false)
  const load = () => api<typeof b>('/api/backup').then(setB).catch(() => {})
  useEffect(() => {
    load()
  }, [])
  const when = (t: number) => new Date(t * 1000).toLocaleString('en-GB', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
  return (
    <>
      <Row
        label="Daily backup"
        hint="Database, settings, spools, print log, Obico records and History pictures, every night at 3:00 to the backups folder (the /backups volume). Timelapses are not included."
      >
        <Toggle checked={s.backup_enabled} onChange={(v) => patch({ backup_enabled: v })} />
      </Row>
      <Row label="Backups kept">
        <input
          key={`bk-${s.backup_keep}`}
          className="input h-9 w-24"
          type="number"
          min={1}
          max={365}
          defaultValue={s.backup_keep}
          onBlur={(e) => {
            const v = Math.max(1, Math.min(365, Math.round(Number(e.target.value) || 14)))
            if (v !== s.backup_keep) patch({ backup_keep: v })
          }}
        />
      </Row>
      {b && !b.available && <p className="text-sm font-semibold text-error">The backup folder ({b.dir}) is not mounted.</p>}
      {b?.last && (
        <p className={`text-sm ${b.last.ok ? 'text-muted' : 'text-error'}`}>
          Last backup: {when(b.last.time)} {b.last.ok ? '' : '(failed)'}
        </p>
      )}
      <button
        className="btn self-start"
        disabled={busy || !b?.available}
        onClick={async () => {
          setBusy(true)
          try {
            const r = await api<{ ok: boolean; message: string }>('/api/backup', { body: {} })
            dlg.toast(r.ok, r.message)
          } catch (e) {
            dlg.toast(false, (e as ApiError).message)
          }
          setBusy(false)
          load()
        }}
      >
        {busy ? 'Saving...' : 'Back up now'}
      </button>
      {b && b.items.length > 0 && (
        <div className="flex flex-col divide-y divide-border text-sm">
          {b.items.slice(0, 5).map((i) => (
            <a key={i.name} href={`/api/backup/${i.name}`} download className="flex items-center gap-2 py-1.5 hover:text-accent">
              <span className="flex-1 truncate font-mono text-xs">{i.name}</span>
              <span className="text-xs text-muted">{Math.max(1, Math.round(i.size / 1024))} KB</span>
            </a>
          ))}
        </div>
      )}
    </>
  )
}

// ---------------------------------------------------------------- maintenance counters

interface MaintItem {
  id: number
  name: string
  type: 'hours' | 'days'
  interval: number
  enabled: boolean
  until: number
}

function MaintenanceSection() {
  const dlg = useDialogs()
  const [data, setData] = useState<{ hours: number; items: MaintItem[] } | null>(null)
  const [add, setAdd] = useState({ name: '', type: 'hours' as 'hours' | 'days', interval: 100 })
  const reload = () => api<{ hours: number; items: MaintItem[] }>('/api/maintenance').then(setData).catch(() => {})
  useEffect(() => {
    reload()
  }, [])
  const call = async (path: string, body?: unknown, method?: string) => {
    try {
      const r = await api<{ ok: boolean; message: string }>(path, { body: body ?? {}, ...(method ? { method } : {}) })
      if (!r.ok) dlg.toast(false, r.message)
      return r.ok
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
      return false
    } finally {
      reload()
    }
  }
  const fmt = (it: MaintItem) =>
    it.until <= 0
      ? `due (${Math.abs(Math.round(it.until))} ${it.type === 'days' ? 'days' : 'h'} over)`
      : `in ${Math.round(it.until)} ${it.type === 'days' ? 'days' : 'h'}`
  return (
    <Section title="Maintenance">
      <p className="-mt-2 text-sm text-muted">Counts the print hours; due items show in the top bar.</p>
      {data && data.items.length === 0 && (
        <button className="btn self-start" onClick={() => call('/api/maintenance/defaults')}>
          Add the usual P1S items
        </button>
      )}
      {data && (
        <>
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted">Total print hours</span>
            <input
              key={data.hours}
              className="input h-8 w-24 tabular-nums"
              type="number"
              min={0}
              defaultValue={data.hours.toFixed(1)}
              onBlur={(e) => Number(e.target.value) !== data.hours && call('/api/maintenance/hours', { hours: Number(e.target.value) })}
            />
            <span className="text-muted">h</span>
          </div>
          <div className="flex flex-col divide-y divide-border">
            {data.items.map((it) => (
              <div key={it.id} className={`flex flex-wrap items-center gap-2 py-2 ${it.enabled ? '' : 'opacity-50'}`}>
                <span className="min-w-0 flex-1 font-semibold">{it.name}</span>
                <span className="text-xs text-muted">
                  every {it.interval} {it.type === 'days' ? 'days' : 'h'}
                </span>
                <span className={`w-36 text-right text-xs font-bold ${it.until <= 0 ? 'text-error' : 'text-muted'}`}>{fmt(it)}</span>
                <button className="btn h-8 px-2 text-xs" onClick={() => call('/api/cmd/maintenance_done', { id: it.id })}>
                  Done
                </button>
                <button
                  className="btn h-8 px-2 text-xs"
                  onClick={() => call('/api/maintenance/item', { id: it.id, name: it.name, type: it.type, interval: it.interval, enabled: !it.enabled })}
                >
                  {it.enabled ? 'Off' : 'On'}
                </button>
                <button
                  className="btn h-8 px-2 text-xs"
                  onClick={async () => (await dlg.confirm({ title: `Delete "${it.name}"?`, text: 'The item and its counter are removed.', ok: 'Delete' })) && call(`/api/maintenance/item/${it.id}`, undefined, 'DELETE')}
                >
                  Delete
                </button>
              </div>
            ))}
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <input className="input min-w-40 flex-1" placeholder="New item, e.g. Lubricate rods" value={add.name} onChange={(e) => setAdd({ ...add, name: e.target.value })} />
            <input className="input w-24" type="number" min={1} value={add.interval} onChange={(e) => setAdd({ ...add, interval: Number(e.target.value) })} />
            <select className="input w-28" value={add.type} onChange={(e) => setAdd({ ...add, type: e.target.value as 'hours' | 'days' })}>
              <option value="hours">print h</option>
              <option value="days">days</option>
            </select>
            <button
              className="btn-accent"
              disabled={!add.name.trim()}
              onClick={async () => (await call('/api/maintenance/item', add)) && setAdd({ ...add, name: '' })}
            >
              Add
            </button>
          </div>
        </>
      )}
    </Section>
  )
}

// ---------------------------------------------------------------- ESP32 panel

function PanelSection({ view, patch, reload }: { view: SettingsView; patch: (c: Patch) => Promise<boolean>; reload: () => void }) {
  const dlg = useDialogs()
  const [shown, setShown] = useState('')
  return (
    <Section title="ESP32 panel">
      <p className="-mt-2 text-sm text-muted">
        The panel talks to this app with an API key (X-API-Key). Either paste the key the panel already has (its web page, Settings), or
        generate a new one here and type it into the panel. Server address for the panel: {window.location.origin}
      </p>
      <SecretField
        label="Panel API key"
        isSet={view.secrets.panel_api_key}
        placeholder="paste the panel's key"
        onSave={(v) => patch({ panel_api_key: v })}
      />
      <button
        className="btn self-start"
        onClick={async () => {
          if (
            view.secrets.panel_api_key &&
            !(await dlg.confirm({ title: 'New panel key?', text: 'The panel stops working until you type the new key into it.', ok: 'Generate' }))
          )
            return
          const r = await api<{ key: string }>('/api/panel-key/generate', { body: {} }).catch((e: ApiError) => {
            dlg.toast(false, e.message)
            return null
          })
          if (r) {
            setShown(r.key)
            reload()
          }
        }}
      >
        Generate a new key
      </button>
      {shown && (
        <div className="rounded-lg border border-accent/50 bg-card2 p-3">
          <div className="mb-1 text-xs text-muted">Shown only now - type it into the panel:</div>
          <div className="flex items-center gap-2">
            <code className="flex-1 font-mono text-sm break-all text-accent select-all">{shown}</code>
            <button className="btn h-8 px-2 text-xs" onClick={() => navigator.clipboard?.writeText(shown).then(() => dlg.toast(true, 'Copied'))}>
              Copy
            </button>
            <button className="btn h-8 px-2 text-xs" onClick={() => setShown('')}>
              Hide
            </button>
          </div>
        </div>
      )}
    </Section>
  )
}

// ---------------------------------------------------------------- printer LAN

function PrinterLanSection({ view, patch }: { view: SettingsView; patch: (c: Patch) => Promise<boolean> }) {
  const s = view.settings
  return (
    <Section title="Printer (LAN)">
      <p className="-mt-2 text-sm text-muted">
        The app talks to the printer over the LAN: status and commands (MQTT), SD card (FTPS) and camera. The access code is on the
        printer: Settings &gt; WLAN. Stored only on the server, never shown again.
      </p>
      <TextField label="Printer IP" value={s.printer_ip} placeholder="192.168.1.50" onSave={(v) => patch({ printer_ip: v })} />
      <TextField label="Serial number" value={s.printer_serial} placeholder="01P00A..." onSave={(v) => patch({ printer_serial: v })} />
      <SecretField
        label="LAN access code"
        isSet={view.secrets.printer_access_code}
        placeholder="8 characters"
        onSave={(v) => patch({ printer_access_code: v })}
      />
      <FromCloudButton />
      <p className={`text-sm font-semibold ${view.mqtt.connected ? 'text-accent' : 'text-error'}`}>
        {view.mqtt.connected
          ? `Connected to the printer (last report ${Math.max(0, Math.round(view.mqtt.last_message_s))} s ago)`
          : `Not connected: ${view.mqtt.error || view.mqtt.link}`}
      </p>
      <p className="-mt-2 text-sm text-muted">
        Heater and fan commands go through Bambu Cloud (log in below): the P1S ignores them over LAN.
      </p>
      {view.ftp.last_error && <p className="text-sm font-semibold text-error">Last FTP error: {view.ftp.last_error}</p>}
      <p className={`text-sm font-semibold ${view.camera.connected ? 'text-accent' : view.camera.last_error ? 'text-error' : 'text-muted'}`}>
        {view.camera.connected
          ? `Camera connected (${(view.camera.fps ?? 0).toFixed(1)} fps)`
          : view.camera.last_error
            ? `Camera: ${view.camera.last_error}`
            : 'Camera idle - connects when something needs a picture'}
        {view.camera.ipcam_record === false && ' · the printer reports its camera as switched off'}
      </p>
      <CameraRestart />
    </Section>
  )
}

/** Takes the access code from the Bambu Cloud account (it is never shown, only stored). */
function FromCloudButton() {
  const dlg = useDialogs()
  const [busy, setBusy] = useState(false)
  return (
    <button
      className="btn self-start"
      disabled={busy}
      onClick={async () => {
        setBusy(true)
        try {
          const r = await api<{ ok: boolean; message: string }>('/api/printer/access-code-from-cloud', { body: {} })
          dlg.toast(r.ok, r.message)
        } catch (e) {
          dlg.toast(false, (e as ApiError).message)
        }
        setBusy(false)
      }}
      title="Needs the Bambu Cloud login above"
    >
      {busy ? 'Reading...' : 'Get access code from Bambu Cloud'}
    </button>
  )
}

/** Editor of the notification texts (Hungarian defaults, kept in notify_texts.json on the server). */
function NotifyTexts() {
  const dlg = useDialogs()
  const [open, setOpen] = useState(false)
  const [data, setData] = useState<{ defaults: Record<string, string>; custom: Record<string, string> } | null>(null)
  const [edit, setEdit] = useState<Record<string, string>>({})
  const [filter, setFilter] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (!open) return
    api<{ defaults: Record<string, string>; custom: Record<string, string> }>('/api/notify/texts')
      .then((d) => {
        setData(d)
        setEdit(d.custom)
      })
      .catch((e: ApiError) => dlg.toast(false, e.message))
  }, [open])
  const save = async () => {
    if (!data) return
    setBusy(true)
    try {
      const body = Object.fromEntries(Object.keys(data.defaults).map((k) => [k, edit[k] ?? '']))
      const r = await api<{ custom: Record<string, string> }>('/api/notify/texts', { method: 'PUT', body })
      setEdit(r.custom)
      dlg.toast(true, 'Texts saved')
      setOpen(false)
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
    }
    setBusy(false)
  }
  const keys = data ? Object.keys(data.defaults).filter((k) => !filter || `${k} ${data.defaults[k]}`.toLowerCase().includes(filter.toLowerCase())) : []
  return (
    <>
      <button className="btn self-start" onClick={() => setOpen(true)}>
        Edit notification texts
      </button>
      {open && (
        <Modal
          title="Notification texts"
          wide
          onClose={() => setOpen(false)}
          footer={
            <button className="btn-accent" disabled={busy || !data} onClick={save}>
              Save
            </button>
          }
        >
          <p className="mb-3 text-sm text-muted">
            Empty = the built-in text (shown greyed). Placeholders in {'{braces}'} are filled in: {'{job}'}, {'{duration}'}, {'{grams}'},{' '}
            {'{percent}'}, {'{tray}'}, {'{spool}'}, {'{reason}'}, ... <b>**bold**</b> works on Discord and ntfy. Kept on the server in
            /data/notify_texts.json.
          </p>
          <input className="input mb-3" placeholder="Search..." value={filter} onChange={(e) => setFilter(e.target.value)} />
          {!data && <p className="text-muted">Loading...</p>}
          <div className="flex flex-col gap-3">
            {keys.map((k) => (
              <label key={k} className="flex flex-col gap-1">
                <span className="font-mono text-xs text-muted">{k}</span>
                <textarea
                  className={`input min-h-10 py-2 text-sm ${edit[k] ? 'border-accent/60' : ''}`}
                  rows={data!.defaults[k].length > 70 ? 2 : 1}
                  placeholder={data!.defaults[k]}
                  value={edit[k] ?? ''}
                  onChange={(e) => setEdit({ ...edit, [k]: e.target.value })}
                />
              </label>
            ))}
          </div>
        </Modal>
      )}
    </>
  )
}

// ---------------------------------------------------------------- Obico

function Seg({ value, options, onChange }: { value: string; options: [string, string][]; onChange: (v: string) => void }) {
  return (
    <div className="flex gap-0.5 rounded-lg bg-card p-0.5">
      {options.map(([v, label]) => (
        <button key={v} onClick={() => onChange(v)} className={`rounded-md px-2.5 py-1 text-xs font-bold ${value === v ? 'bg-card2 text-accent' : 'text-muted'}`}>
          {label}
        </button>
      ))}
    </div>
  )
}

function ObicoSection({ view, patch }: { view: SettingsView; patch: (c: Patch) => Promise<boolean> }) {
  const dlg = useDialogs()
  const s = view.settings
  const [testing, setTesting] = useState(false)
  const test = async () => {
    setTesting(true)
    try {
      const r = await api<{ ok: boolean; error: string | null; status_code: number | null }>('/api/obico/test', { body: {} })
      dlg.toast(r.ok, r.ok ? 'Obico ML server reachable' : r.error || `HTTP ${r.status_code ?? '?'}`)
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
    }
    setTesting(false)
  }
  return (
    <Section title="Obico failure detection">
      <Row
        label="Run Obico from this app"
        hint="Sends a camera frame to the Obico ML server every few seconds while printing."
      >
        <Toggle checked={s.obico_enabled} onChange={(v) => patch({ obico_enabled: v })} />
      </Row>
      <Row label="Sensitivity" hint="High warns earlier, with more false alarms">
        <Seg value={s.obico_sensitivity} options={[['low', 'Low'], ['medium', 'Medium'], ['high', 'High']]} onChange={(v) => patch({ obico_sensitivity: v })} />
      </Row>
      <Row label="On failure" hint="Pause stops the print until you resume it">
        <Seg value={s.obico_action} options={[['notify', 'Notify'], ['pause', 'Pause']]} onChange={(v) => patch({ obico_action: v })} />
      </Row>
      <TextField label="ML server URL" value={s.obico_ml_url} placeholder="http://<server>:3333" onSave={(v) => patch({ obico_ml_url: v })} />
      <TextField
        label="This app's address (for the ML server)"
        value={s.obico_frame_base_url}
        placeholder={window.location.origin}
        hint="The ML server downloads the frame from here, so it must be reachable from that machine."
        onSave={(v) => patch({ obico_frame_base_url: v })}
      />
      <SecretField
        label="ML API token (optional)"
        isSet={view.secrets.obico_ml_token}
        placeholder="only if the ML server runs with ML_API_TOKEN"
        onSave={(v) => patch({ obico_ml_token: v })}
      />
      <button className="btn self-start" disabled={testing} onClick={test}>
        {testing ? 'Testing...' : 'Test ML server'}
      </button>
    </Section>
  )
}

// ---------------------------------------------------------------- notifications

function NotifySection({ view, patch }: { view: SettingsView; patch: (c: Patch) => Promise<boolean> }) {
  const dlg = useDialogs()
  const s = view.settings
  const [testing, setTesting] = useState('')
  const test = async (channel: 'discord' | 'ntfy') => {
    setTesting(channel)
    try {
      const res = await api<Record<string, { ok: boolean; detail: string }>>('/api/notify/test', { body: { channel } })
      const r = Object.values(res)[0]
      dlg.toast(!!r?.ok, r?.ok ? 'Test sent - check your phone' : r?.detail || 'Failed')
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
    }
    setTesting('')
  }
  return (
    <Section title="Notifications">
      <Row label="Discord" hint="Webhook of a channel: Server settings > Integrations > Webhooks">
        <Toggle checked={s.notify_discord_enabled} onChange={(v) => patch({ notify_discord_enabled: v })} />
      </Row>
      <SecretField
        label="Discord webhook URL"
        isSet={view.secrets.discord_webhook_url}
        placeholder="https://discord.com/api/webhooks/..."
        onSave={(v) => patch({ discord_webhook_url: v })}
      />
      <button className="btn self-start" disabled={!view.secrets.discord_webhook_url || !!testing} onClick={() => test('discord')}>
        {testing === 'discord' ? 'Sending...' : 'Send Discord test'}
      </button>

      <div className="border-t border-border" />
      <Row label="ntfy" hint="Push to the ntfy app (ntfy.sh or your own server)">
        <Toggle checked={s.notify_ntfy_enabled} onChange={(v) => patch({ notify_ntfy_enabled: v })} />
      </Row>
      <TextField label="ntfy server" value={s.ntfy_server} placeholder="https://ntfy.sh" onSave={(v) => patch({ ntfy_server: v })} />
      <TextField
        label="ntfy topic"
        value={s.ntfy_topic}
        placeholder="my-printer-1234"
        hint="On ntfy.sh anyone who knows the topic can read it: pick a long, random one."
        onSave={(v) => patch({ ntfy_topic: v })}
      />
      <SecretField
        label="ntfy access token (optional)"
        isSet={view.secrets.ntfy_token}
        placeholder="tk_... (only for protected topics)"
        onSave={(v) => patch({ ntfy_token: v })}
      />
      <button className="btn self-start" disabled={!s.ntfy_topic || !!testing} onClick={() => test('ntfy')}>
        {testing === 'ntfy' ? 'Sending...' : 'Send ntfy test'}
      </button>

      <div className="border-t border-border" />
      <TextField
        label="App address in pushes"
        value={s.notify_app_url}
        placeholder={window.location.origin}
        hint="A tap on the notification opens this."
        onSave={(v) => patch({ notify_app_url: v })}
      />
      <Row label="Summary" hint="Prints, time, filament, cost, spools used, low spools and due maintenance">
        <Seg
          value={s.summary_mode}
          options={[
            ['off', 'Off'],
            ['daily', 'Daily'],
            ['weekly', 'Weekly (Sun)'],
          ]}
          onChange={(v) => patch({ summary_mode: v })}
        />
      </Row>
      {s.summary_mode !== 'off' && (
        <Row label="Summary at (hour)">
          <div className="flex items-center gap-2">
            <input
              key={`sh-${s.summary_hour}`}
              className="input h-9 w-20"
              type="number"
              min={0}
              max={23}
              defaultValue={s.summary_hour}
              onBlur={(e) => {
                const v = Math.max(0, Math.min(23, Math.round(Number(e.target.value) || 0)))
                if (v !== s.summary_hour) patch({ summary_hour: v })
              }}
            />
            <button
              className="btn h-9 text-sm"
              onClick={() =>
                api<{ ok: boolean; message: string }>('/api/notify/summary', { body: {} })
                  .then((r) => dlg.toast(r.ok, r.message))
                  .catch((e: ApiError) => dlg.toast(false, e.message))
              }
            >
              Send now
            </button>
          </div>
        </Row>
      )}
      <div className="border-t border-border" />
      <div className="font-semibold">Which channel gets what</div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-xs text-muted">
              <th className="py-1 text-left font-semibold">Event</th>
              <th className="w-20 py-1 font-semibold">Discord</th>
              <th className="w-20 py-1 font-semibold">ntfy</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {view.notify_events.map((e) => (
              <tr key={e.key}>
                <td className="py-2 pr-2">{e.label}</td>
                {(['discord', 'ntfy'] as const).map((ch) => (
                  <td key={ch} className="py-2 text-center">
                    <span className="inline-flex">
                      <Toggle
                        checked={!!s[`notify_${ch}_on_${e.key}` as `notify_on_${string}`]}
                        onChange={(v) => patch({ [`notify_${ch}_on_${e.key}`]: v })}
                      />
                    </span>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <NotifyTexts />
    </Section>
  )
}

// ---------------------------------------------------------------- Bambu Cloud

type CloudStep = { step: 'form' } | { step: 'email_code'; email: string; china: boolean } | { step: 'totp'; email: string; china: boolean; tfa_key: string }

function CloudSection({ cloud }: { cloud: CloudInfo }) {
  const dlg = useDialogs()
  const [step, setStep] = useState<CloudStep>({ step: 'form' })
  const [email, setEmail] = useState(cloud.email)
  const [password, setPassword] = useState('')
  const [china, setChina] = useState(cloud.china)
  const [code, setCode] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const run = async (path: string, body: Record<string, unknown>) => {
    setBusy(true)
    setMsg(null)
    try {
      const r = await api<{ step: string; message: string; tfa_key?: string }>(path, { body })
      if (r.step === 'done') {
        setStep({ step: 'form' })
        setPassword('')
        setCode('')
        setToken('')
        dlg.toast(true, `Bambu Cloud: ${r.message}`)
      } else if (r.step === 'email_code') {
        setStep({ step: 'email_code', email, china })
        setMsg({ ok: true, text: r.message })
      } else if (r.step === 'totp') {
        setStep({ step: 'totp', email, china, tfa_key: r.tfa_key ?? '' })
        setMsg({ ok: true, text: r.message })
      } else {
        setMsg({ ok: false, text: `Bambu Cloud login failed: ${r.message}` })
      }
    } catch (e) {
      setMsg({ ok: false, text: (e as ApiError).message })
    } finally {
      setBusy(false)
    }
  }

  if (cloud.logged_in) {
    return (
      <Section title="Bambu Cloud account">
        <p>
          Logged in as <b>{cloud.email || '(token)'}</b>
          {cloud.china && ' (China region)'}
          {cloud.expiry && (
            <span className={cloud.expiry_warn ? 'font-bold text-warn' : 'text-muted'}>, {cloud.expiry}</span>
          )}
        </p>
        <p className="text-sm text-muted">Used to start SD card prints and reprints (the printer is in cloud mode).</p>
        <div>
          <button
            className="btn"
            onClick={async () => {
              if (await dlg.confirm({ title: 'Log out?', text: 'Log out of Bambu Cloud on this server?', ok: 'Log out', tone: 'danger' })) {
                const r = await api<{ message: string }>('/api/cloud/logout', { body: {} })
                dlg.toast(true, r.message)
              }
            }}
          >
            Log out
          </button>
        </div>
      </Section>
    )
  }

  return (
    <Section title="Bambu Cloud account">
      <p className="text-sm text-muted">
        Needed to print from the SD card and to reprint (cloud mode). The password is only sent to Bambu; the server keeps the
        token.
      </p>
      {step.step === 'form' && (
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            run('/api/cloud/login', { email, password, china })
          }}
        >
          <div>
            <label className="label">Email</label>
            <input className="input" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div>
            <label className="label">Password</label>
            <input
              className="input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          <div>
            <label className="label">Region</label>
            <select className="input" value={china ? '1' : '0'} onChange={(e) => setChina(e.target.value === '1')}>
              <option value="0">Global (bambulab.com)</option>
              <option value="1">China (bambulab.cn)</option>
            </select>
          </div>
          <div>
            <button className="btn-accent" disabled={busy || !email || !password}>
              {busy ? 'Logging in...' : 'Log in'}
            </button>
          </div>
        </form>
      )}
      {step.step !== 'form' && (
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            if (step.step === 'email_code') run('/api/cloud/code', { email: step.email, code, china: step.china })
            else run('/api/cloud/totp', { email: step.email, code, china: step.china, tfa_key: step.tfa_key })
          }}
        >
          <div>
            <label className="label">{step.step === 'email_code' ? 'Verification code from the email' : 'Authenticator code'}</label>
            <input
              className="input tracking-[0.3em]"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoFocus
            />
          </div>
          <div className="flex gap-2">
            <button className="btn-accent" disabled={busy || !code}>
              {busy ? 'Checking...' : 'Verify'}
            </button>
            <button type="button" className="btn" onClick={() => setStep({ step: 'form' })}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {msg && <p className={`text-sm font-semibold ${msg.ok ? 'text-info' : 'text-error'}`}>{msg.text}</p>}
      <details className="rounded-xl bg-panel p-3">
        <summary className="cursor-pointer text-sm font-semibold text-muted">Or paste an access token</summary>
        <form
          className="mt-3 flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault()
            run('/api/cloud/token', { token, email, china })
          }}
        >
          <p className="text-sm text-muted">The "token" cookie of a logged-in bambulab.com browser session.</p>
          <input className="input" type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} placeholder="Token" />
          <div>
            <button className="btn" disabled={busy || token.length < 20}>
              Save token
            </button>
          </div>
        </form>
      </details>
    </Section>
  )
}

// ---------------------------------------------------------------- account

function AccountSection({ onLogout }: { onLogout: () => void }) {
  const dlg = useDialogs()
  const [cur, setCur] = useState('')
  const [next, setNext] = useState('')
  return (
    <Section title="App login">
      <form
        className="flex flex-col gap-3 sm:flex-row sm:items-end"
        onSubmit={async (e) => {
          e.preventDefault()
          try {
            await api('/api/auth/password', { body: { current: cur, password: next } })
            setCur('')
            setNext('')
            dlg.toast(true, 'Password changed - other devices are logged out')
          } catch (err) {
            dlg.toast(false, (err as ApiError).message)
          }
        }}
      >
        <div className="flex-1">
          <label className="label">Current password</label>
          <input className="input" type="password" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} />
        </div>
        <div className="flex-1">
          <label className="label">New password / PIN</label>
          <input className="input" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} />
        </div>
        <button className="btn" disabled={!cur || next.length < 4}>
          Change
        </button>
      </form>
      <div>
        <button
          className="btn"
          onClick={async () => {
            await api('/api/auth/logout', { body: {} }).catch(() => {})
            onLogout()
          }}
        >
          Log out of this device
        </button>
      </div>
    </Section>
  )
}
