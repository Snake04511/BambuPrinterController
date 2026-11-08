// Print start UI shared by FILES (SD card) and HOME (Reprint): file / cloud job dialogs,
// filament chips with tray override, forecast and the print summary
// (panel src/ui/pages/FilesPage.cpp openSdFile / openCloudTask / openSummary).
import { ArrowRight, Download, Image as ImageIcon, Loader2, Play, RefreshCw, Trash2, Zap } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  applyOverrides,
  computeAmsMap,
  FORECAST_TEXT,
  forecast,
  loadedTrays,
  MAP_EXTERNAL,
  MAP_MISSING,
  MAP_UNUSED,
  trayById,
  trayLabel,
  type ForecastRow,
  type PlateFilament,
} from './ams'
import { api, cmd } from './api'
import { C, formatEta, isPrinting, SPEED_NAMES } from './format'
import type { Snapshot } from './types'
import { startDownload, useDownloads } from './downloads'
import { Modal, Swatch, Toggle, useDialogs, type Choice } from './ui'

// ---------------------------------------------------------------- types / helpers

export interface SdFile {
  name: string
  size: number
  date: string
  print_sec: number
  grams: number
  plates: number
  thumb: boolean
  failed: boolean
}

export interface SdPlate {
  index: number
  name: string
  print_sec: number
  grams: number
  has_thumb: boolean
  filaments: PlateFilament[]
}

interface SdDetail {
  name: string
  size: number
  plates: SdPlate[]
}

export interface CloudTask {
  id: string
  title: string
  start_time: string
  cost_sec: number
  plate_index: number
  has_cover: boolean
}

interface CloudDetail {
  ok: boolean
  error?: string
  rows: { weight: number; filament_type: string; source_color: string }[]
  ams_mapping: number[]
}

export function fmtSec(sec: number): string {
  if (sec < 0) return '--'
  const min = Math.ceil(sec / 60)
  return min >= 60 ? `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, '0')}m` : `${min} min`
}

export function fmtSize(b: number): string {
  return b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.ceil(b / 1024)} KB`
}

export function sdThumbUrl(name: string, size: number, plate = 1, bust = ''): string {
  return `/api/sd/thumb?name=${encodeURIComponent(name)}&size=${size}&plate=${plate}${bust ? `&b=${bust}` : ''}`
}

/** Panel's normJob: lowercase alphanumerics, extension stripped. */
export function normJob(s: string): string {
  return s
    .toLowerCase()
    .replace(/\.gcode\.3mf$|\.3mf$|\.gcode$/, '')
    .replace(/[^0-9a-z]/g, '')
}

/** Same rules as the panel: exact normalized match first, then containment (both >= 4 chars). */
export function findByJob<T>(items: T[], job: string, nameOf: (t: T) => string): T | null {
  const j = normJob(job)
  if (!j) return null
  const exact = items.find((t) => normJob(nameOf(t)) === j)
  if (exact) return exact
  if (j.length < 4) return null
  return (
    items.find((t) => {
      const n = normJob(nameOf(t))
      return n.length >= 4 && (n.includes(j) || j.includes(n))
    }) ?? null
  )
}

function Img({ src, className, onClick }: { src: string | null; className?: string; onClick?: () => void }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [src])
  if (!src || failed)
    return (
      <div className={`flex items-center justify-center rounded-xl bg-panel text-card2 ${className ?? ''}`}>
        <ImageIcon className="h-1/3 w-1/3" />
      </div>
    )
  return (
    <img
      src={src}
      alt=""
      onError={() => setFailed(true)}
      onClick={onClick}
      className={`rounded-xl bg-panel object-contain ${onClick ? 'cursor-zoom-in' : ''} ${className ?? ''}`}
    />
  )
}

function ImageZoom({ src, title, info, onClose }: { src: string; title: string; info?: string; onClose: () => void }) {
  return (
    <Modal title={title} onClose={onClose} wide>
      <Img src={src} className="mx-auto aspect-square w-full max-w-[640px]" />
      {info && <p className="mt-3 text-center text-sm whitespace-pre-line text-muted">{info}</p>}
    </Modal>
  )
}

// ---------------------------------------------------------------- filament chips (tray override)

function FilamentChips({
  snap,
  filaments,
  map,
  overrides,
  setOverride,
}: {
  snap: Snapshot
  filaments: PlateFilament[]
  map: number[]
  overrides: Record<number, number>
  setOverride: (slot: number, tray: number | undefined) => void
}) {
  const dlg = useDialogs()
  const s = snap.status!
  const pick = async (f: PlateFilament) => {
    const trays = loadedTrays(s)
    const cur = overrides[f.slot - 1]
    const choices: Choice<number | 'auto'>[] = [
      { label: 'Auto (matching type / colour)', value: 'auto', selected: cur === undefined },
      ...trays.map((t) => ({
        label: (
          <span className="flex items-center gap-3">
            <Swatch color={t.tray.color} />
            <span className="w-10 font-bold">{t.id === MAP_EXTERNAL ? 'Ext' : t.label}</span>
            <span>{t.tray.type}</span>
            <span className="truncate text-muted">{t.tray.sub_brands}</span>
          </span>
        ),
        value: t.id,
        selected: cur === t.id,
      })),
    ]
    const v = await dlg.choose({ title: `Filament ${f.slot}: ${f.type} ${f.color}`, choices })
    if (v === null) return
    setOverride(f.slot, v === 'auto' ? undefined : v)
  }
  return (
    <div className="flex flex-col gap-2">
      {filaments.map((f) => {
        const t = map[f.slot - 1]
        if (t === undefined || t === MAP_UNUSED) return null
        const tray = t >= 0 ? trayById(s, t) : null
        const missing = t === MAP_MISSING || !tray
        const manual = overrides[f.slot - 1] !== undefined
        return (
          <button
            key={f.slot}
            onClick={() => pick(f)}
            className={`flex min-h-12 items-center gap-2 rounded-xl px-3 py-2 text-left text-sm transition hover:brightness-110 ${missing ? 'bg-[#5A1F1F]' : 'bg-card2'}`}
          >
            <Swatch color={f.color} />
            <span className="font-bold">{f.type || '?'}</span>
            {f.grams > 0 && <span className="text-muted">{f.grams.toFixed(1)} g</span>}
            <ArrowRight size={16} className="mx-1 shrink-0 text-muted" />
            {missing ? (
              <span className="font-semibold text-error">not loaded - tap to pick a tray</span>
            ) : (
              <>
                <span className="font-bold">{trayLabel(t)}</span>
                <Swatch color={tray.color} />
                <span className="truncate">{tray.sub_brands || tray.type}</span>
                {manual && <span className="text-warn">*</span>}
              </>
            )}
            <span className="ml-auto pl-2 text-muted">✎</span>
          </button>
        )
      })}
    </div>
  )
}

function stateColor(r: ForecastRow): string {
  return r.state === 'short' || r.state === 'missing' ? C.error : r.state === 'low' ? C.warn : r.state === 'enough' ? C.accent : C.muted
}

/** A tray has less than the print needs: say so plainly before the start (the printer
 * would pause at the runout, mid-print). */
async function confirmShort(dlg: ReturnType<typeof useDialogs>, rows: ForecastRow[]): Promise<boolean> {
  const short = rows.filter((r) => r.state === 'short')
  if (!short.length) return true
  const lines = short
    .map((r) => `${r.label}: needs ${Math.round(r.need ?? 0)} g, has ${r.now !== null ? Math.round(r.now) : '?'} g (missing ${r.after !== null ? Math.round(-r.after) : '?'} g)`)
    .join('\n')
  return dlg.confirm({
    title: 'Not enough filament',
    text: `${lines}\n\nThe printer pauses when the spool runs out; you can swap it then. Start anyway?`,
    ok: 'Start anyway',
    tone: 'danger',
  })
}

function ForecastLines({ rows }: { rows: ForecastRow[] }) {
  if (!rows.length) return null
  const short = rows.filter((r) => r.state === 'short')
  return (
    <div className="flex flex-col gap-1 rounded-xl bg-panel px-3 py-2 text-sm">
      {short.length > 0 && (
        <div className="-mx-1 mb-1 rounded-lg bg-error/15 px-2 py-1.5 text-sm font-bold text-error">
          Not enough filament on {short.map((r) => r.label).join(', ')} - the print will pause at the runout
        </div>
      )}
      {rows.map((r, i) => (
        <div key={i} className="flex flex-wrap items-center gap-x-2">
          <span className="w-10 font-bold">{r.label}</span>
          {r.state === 'missing' ? null : (
            <span className="text-muted tabular-nums">
              {r.now !== null ? `${Math.round(r.now)} g` : '? g'} → {r.after !== null ? `${Math.round(r.after)} g after` : '?'}
            </span>
          )}
          <span className="ml-auto font-semibold" style={{ color: stateColor(r) }}>
            {FORECAST_TEXT[r.state]}
            {r.state === 'short' && r.after !== null && ` - missing ${Math.round(-r.after)} g`}
          </span>
        </div>
      ))}
    </div>
  )
}

function StartStatus({ snap, file, since }: { snap: Snapshot; file: string; since: number }) {
  const st = snap.start as { phase: string; file: string; message: string; t?: number }
  if (st.phase === 'idle' || st.file !== file || (st.t ?? 0) * 1000 < since) return null
  const color = st.phase === 'failed' ? C.error : st.phase === 'started' ? C.accent : C.info
  return (
    <p className="flex items-center gap-2 text-sm font-semibold" style={{ color }}>
      {(st.phase === 'sending' || st.phase === 'waiting') && <Loader2 size={16} className="animate-spin" />}
      {st.message}
    </p>
  )
}

/** Closes the dialog and reports when "our" start reaches Started. */
function useStartedWatcher(snap: Snapshot, file: string, since: number, onStarted: () => void) {
  const st = snap.start as { phase: string; file: string; t?: number }
  const fired = useRef(false)
  useEffect(() => {
    if (!fired.current && st.phase === 'started' && st.file === file && (st.t ?? 0) * 1000 >= since) {
      fired.current = true
      onStarted()
    }
  }, [st.phase, st.file, st.t, file, since, onStarted])
}

function startBusy(snap: Snapshot): boolean {
  const p = (snap.start as { phase: string }).phase
  return p === 'sending' || p === 'waiting'
}

// ---------------------------------------------------------------- print summary

function readiness(snap: Snapshot): { text: string; ok: boolean } {
  const s = snap.status
  if (snap.link !== 'online' || !s) return { text: snap.link_text || 'Printer not connected', ok: false }
  if (!s.connected) return { text: 'Printer offline', ok: false }
  if (isPrinting(s)) return { text: `Printer busy (${s.state})`, ok: false }
  return { text: `✓ Printer ready (${s.state || 'IDLE'})`, ok: true }
}

function PrintSummary({
  snap,
  title,
  preview,
  printSec,
  filaments,
  map,
  overrides,
  warning,
  printOptions,
  onStart,
  onClose,
}: {
  snap: Snapshot
  title: string
  preview: string | null
  printSec: number
  filaments: PlateFilament[]
  map: number[]
  overrides: Record<number, number>
  warning?: string
  printOptions: boolean
  onStart: () => void
  onClose: () => void
}) {
  const dlg = useDialogs()
  const [started, setStarted] = useState(false)
  useEffect(() => {
    cmd('refresh_inventory')
  }, [])
  const s = snap.status!
  const cfg = snap.settings
  const rows = forecast(s, snap.assignments, filaments, map, overrides, snap.filament_use?.by_tray ?? {})
  const ready = readiness(snap)
  const eta = printSec > 0 ? formatEta(Math.ceil(printSec / 60)) : null
  const total = filaments.reduce((a, f) => a + (f.grams > 0 ? f.grams : 0), 0)
  const allKnown = filaments.every((f) => f.grams > 0)
  const patch = (c: Record<string, unknown>) => api('/api/settings', { method: 'PATCH', body: c }).catch(() => {})

  const pickSpeed = async () => {
    const v = await dlg.choose({
      title: 'Speed after layer 1',
      choices: [
        { label: 'Off (keep the start speed)', value: 0, selected: cfg.print_speed_l1 === 0 },
        ...[1, 2, 3, 4].map((m) => ({ label: SPEED_NAMES[m], value: m, selected: cfg.print_speed_l1 === m })),
      ],
    })
    if (v !== null) patch({ print_speed_l1: v })
  }

  const option = (label: string, key: 'print_timelapse' | 'print_bed_level' | 'print_flow_cali') => (
    <label className="flex items-center justify-between gap-3 rounded-xl bg-panel px-3 py-2.5">
      <span className="text-sm font-semibold">{label}</span>
      <Toggle checked={cfg[key]} onChange={(v) => patch({ [key]: v })} />
    </label>
  )

  return (
    <Modal
      title="Print summary"
      onClose={onClose}
      wide
      footer={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn-accent min-w-44"
            disabled={started || !ready.ok}
            onClick={() => {
              setStarted(true)
              onStart()
              onClose()
            }}
          >
            <Play size={18} /> START PRINT
          </button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="flex gap-4">
          <Img src={preview} className="aspect-square w-24 shrink-0 sm:w-28" />
          <div className="min-w-0 flex-1">
            <div className="line-clamp-2 font-bold [overflow-wrap:anywhere]">{title}</div>
            <div className="mt-1 text-sm text-dim">
              Estimated time <b>{printSec > 0 ? `~${fmtSec(printSec)}` : 'not available'}</b>
              {eta && (
                <>
                  {' '}
                  · finishes <b>{eta}</b>
                </>
              )}
            </div>
            <div className={`mt-1 text-sm font-semibold ${ready.ok ? 'text-accent' : 'text-error'}`}>{ready.text}</div>
          </div>
        </div>
        {warning && <p className="rounded-xl bg-warn/12 px-3 py-2 text-sm font-semibold text-warn">{warning}</p>}

        <div>
          <div className="section-title mb-0.5">Filament</div>
          <p className="mb-2 text-xs text-muted">Estimate - the spools are booked when the print ends.</p>
          {filaments.length === 0 ? (
            <p className="text-sm text-muted">Filament usage not available for this print.</p>
          ) : (
            <div className="flex flex-col gap-2">
              {rows.map((r, i) => (
                <div key={i} className="flex items-center gap-3 rounded-xl border-l-4 bg-panel px-3 py-2.5" style={{ borderColor: stateColor(r) }}>
                  <Swatch color={r.tray?.color || r.fileColor} size={22} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm font-bold">
                      {r.auto && r.state !== 'missing' ? '~' : ''}
                      {r.label} {r.tray ? r.tray.sub_brands || r.tray.type : ''}
                    </div>
                    <div className="truncate text-xs text-muted">
                      {r.spool
                        ? `Spool #${r.spool.id} ${r.spool.brand} ${r.spool.subtype || r.spool.material} ${r.spool.color_name}`
                        : r.state === 'missing'
                          ? 'No tray chosen'
                          : 'No spool assigned to this tray'}
                    </div>
                  </div>
                  <div className="text-right text-xs tabular-nums">
                    <div>
                      Need {r.need !== null ? `${Math.round(r.need)} g` : 'n/a'} · Now {r.now !== null ? `${Math.round(r.now)} g` : '?'}
                    </div>
                    <div className="font-bold" style={{ color: stateColor(r) }}>
                      {r.after !== null ? `After ${Math.round(r.after)} g · ` : ''}
                      {FORECAST_TEXT[r.state]}
                    </div>
                  </div>
                </div>
              ))}
              <p className="text-xs text-muted">
                {allKnown
                  ? `Total filament needed: ${Math.round(total)} g (estimate from the sliced file)`
                  : 'Total filament needed: not available for every filament'}
              </p>
            </div>
          )}
        </div>

        <div className={`grid gap-2 ${printOptions ? 'sm:grid-cols-2' : ''}`}>
          {printOptions && (
            <>
              {option('Timelapse', 'print_timelapse')}
              {option('Bed leveling', 'print_bed_level')}
              {option('Flow calibration', 'print_flow_cali')}
            </>
          )}
          <button className="flex items-center justify-between gap-3 rounded-xl bg-panel px-3 py-2.5 text-left" onClick={pickSpeed}>
            <span className="text-sm font-semibold">
              <Zap size={14} className="mr-1 inline text-warn" />
              After layer 1
            </span>
            <span className={`text-sm font-bold ${cfg.print_speed_l1 ? 'text-accent' : 'text-muted'}`}>
              {cfg.print_speed_l1 ? SPEED_NAMES[cfg.print_speed_l1] : 'Off'} ✎
            </span>
          </button>
        </div>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------- SD file dialog

/** The file itself to this device: runs in the downloads tray (keeps going when the dialog closes). */
function DownloadButton({ file }: { file: SdFile }) {
  const dl = useDownloads().find((d) => d.name === file.name && d.state !== 'failed')
  const busy = !!dl && dl.state !== 'ready'
  return (
    <button className="btn" onClick={() => startDownload(file.name, file.size)} disabled={busy} title="Download the file (copied from the printer first)">
      {busy ? <Loader2 size={18} className="animate-spin" /> : <Download size={18} />}
      <span className="hidden sm:inline">{busy ? 'Downloading...' : 'Download'}</span>
    </button>
  )
}

export function SdFileDialog({
  snap,
  file,
  onClose,
  onStarted,
}: {
  snap: Snapshot
  file: SdFile
  onClose: () => void
  onStarted: () => void
}) {
  const dlg = useDialogs()
  const [since] = useState(() => Date.now())
  const [res, setRes] = useState<{ state: string; detail?: SdDetail; error?: string } | null>(null)
  const [plateIdx, setPlateIdx] = useState(0)
  const [seqTo, setSeqTo] = useState<number | null>(null) // multi-plate run: last plate index (in plates[])
  const [overrides, setOverrides] = useState<Record<number, number>>({})
  const [summary, setSummary] = useState(false)
  const [zoom, setZoom] = useState(false)
  const [force, setForce] = useState(false)

  useEffect(() => {
    let alive = true
    let timer: number | undefined
    const poll = (first: boolean) =>
      api<{ state: string; detail?: SdDetail; error?: string }>(
        `/api/sd/detail?name=${encodeURIComponent(file.name)}&size=${file.size}${first && force ? '&force=true' : ''}`,
      )
        .then((r) => {
          if (!alive) return
          setRes(r)
          if (r.state !== 'ready' && r.state !== 'error') timer = window.setTimeout(() => poll(false), 1500)
        })
        .catch(() => alive && (timer = window.setTimeout(() => poll(false), 3000)))
    poll(true)
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [file.name, file.size, force])

  useStartedWatcher(snap, file.name, since, () => {
    dlg.toast(true, 'Print started')
    onStarted()
  })

  const detail = res?.state === 'ready' ? res.detail! : null
  const plate = detail?.plates[Math.min(plateIdx, detail.plates.length - 1)]
  // Multi-plate run: this plate, then the next ones (each started by a button). One tray
  // mapping for all of them: the filaments of every plate in the run.
  const runPlates = detail && plate && seqTo !== null && seqTo > plateIdx ? detail.plates.slice(plateIdx, seqTo + 1) : plate ? [plate] : []
  const fils = useMemo(() => {
    const bySlot = new Map<number, PlateFilament>()
    for (const p of runPlates)
      for (const f of p.filaments) {
        const cur = bySlot.get(f.slot)
        if (!cur) bySlot.set(f.slot, { ...f })
        else if (cur.grams > 0 && f.grams > 0) cur.grams += f.grams // the whole run needs it
      }
    return [...bySlot.values()].sort((a, b) => a.slot - b.slot)
  }, [runPlates.map((p) => p.index).join(',')])
  const s = snap.status!
  const auto = useMemo(() => (plate ? computeAmsMap(s, fils) : []), [plate, fils, s])
  const map = applyOverrides(auto, overrides)
  const rows = plate ? forecast(s, snap.assignments, fils, map, overrides, snap.filament_use?.by_tray ?? {}) : []
  const preview = sdThumbUrl(file.name, file.size, plate?.index ?? 1, detail ? 'd' : file.thumb ? 't' : '')
  const cloudOk = snap.cloud.logged_in
  const canPrint = !!plate && cloudOk && !startBusy(snap) && snap.status?.connected && !isPrinting(s)

  const start = async () => {
    if (!plate) return
    if (map.some((t) => t === MAP_MISSING)) {
      dlg.toast(false, 'A required filament is not loaded - pick a tray for it')
      return
    }
    if (!(await confirmShort(dlg, rows))) return
    setSummary(true)
  }
  const doStart = () => {
    const cfg = snap.settings
    api('/api/sd/print', {
      body: {
        name: file.name,
        plate: plate!.index,
        trays: map,
        then: runPlates.slice(1).map((p) => p.index),
        names: Object.fromEntries(runPlates.slice(1).map((p) => [p.index, p.name || ''])),
        timelapse: cfg.print_timelapse,
        bed_levelling: cfg.print_bed_level,
        flow_cali: cfg.print_flow_cali,
        speed_l1: cfg.print_speed_l1,
      },
    }).catch((e) => dlg.toast(false, e.message))
  }
  const del = async () => {
    const ok = await dlg.confirm({
      title: 'Delete file?',
      text: `Delete "${file.name}" from the printer's SD card? This cannot be undone.`,
      ok: 'Delete',
      tone: 'danger',
    })
    if (!ok) return
    const r = await api<{ ok: boolean }>('/api/sd/delete', { body: { name: file.name } }).catch(() => ({ ok: false }))
    if (r.ok) onClose()
  }

  let info: ReactNode
  if (!res || res.state === 'loading' || res.state === 'queued') {
    info = (
      <p className="flex items-start gap-2 text-dim">
        <Loader2 size={18} className="mt-0.5 shrink-0 animate-spin text-accent" />
        {res?.state === 'queued'
          ? 'Waiting for another download from the printer (the printer serves one file at a time)...'
          : 'Reading the file from the printer... (big files can take a few minutes)'}
      </p>
    )
  } else if (res.state === 'error') {
    info = (
      <div className="flex flex-col items-start gap-2">
        <p className="text-error">{res.error || 'No plate information for this file'}</p>
        <button className="btn" onClick={() => setForce((f) => !f)}>
          <RefreshCw size={16} /> Try again
        </button>
      </div>
    )
  } else if (plate) {
    const grams = plate.grams > 0 ? plate.grams : plate.filaments.reduce((a, f) => a + f.grams, 0)
    info = (
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted">Print time</dt>
        <dd className="font-semibold">~{fmtSec(plate.print_sec)}</dd>
        <dt className="text-muted">Filament</dt>
        <dd className="font-semibold">{grams > 0 ? `${grams.toFixed(1)} g` : '--'}</dd>
        <dt className="text-muted">Plate</dt>
        <dd className="font-semibold">
          {plate.index}
          {plate.name && <span className="font-normal text-muted"> · {plate.name}</span>}
        </dd>
        <dt className="text-muted">Saved</dt>
        <dd>
          {file.date} <span className="text-muted">({fmtSize(file.size)})</span>
        </dd>
      </dl>
    )
  }

  return (
    <>
      <Modal
        title={file.name}
        onClose={onClose}
        wide
        footer={
          <>
            <button className="btn text-error" onClick={del} title="Delete from the SD card">
              <Trash2 size={18} /> Delete
            </button>
            <DownloadButton file={file} />
            <span className="flex-1" />
            <button className="btn-accent min-w-36" disabled={!canPrint} onClick={start}>
              <Play size={18} /> PRINT
            </button>
          </>
        }
      >
        <div className="flex flex-col gap-4 sm:flex-row">
          <div className="mx-auto w-3/5 max-w-[240px] sm:mx-0 sm:w-56 sm:max-w-none sm:shrink-0">
            <Img src={preview} className="aspect-square w-full" onClick={() => setZoom(true)} />
            {detail && detail.plates.length > 1 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {detail.plates.map((p, i) => (
                  <button
                    key={p.index}
                    onClick={() => {
                      setPlateIdx(i)
                      setOverrides({})
                      setSeqTo(null)
                    }}
                    className={`h-9 min-w-10 rounded-lg px-2 text-sm font-bold ${i === plateIdx ? 'bg-accent text-black' : 'bg-card2 text-muted'}`}
                  >
                    {p.index}
                  </button>
                ))}
              </div>
            )}
            {detail && detail.plates.length > 1 && plateIdx < detail.plates.length - 1 && (
              <div className="mt-2 rounded-lg bg-card2 p-2 text-sm">
                <label className="flex cursor-pointer items-center gap-2 font-semibold">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-[var(--color-accent)]"
                    checked={seqTo !== null}
                    onChange={(e) => {
                      setSeqTo(e.target.checked ? detail.plates.length - 1 : null)
                      setOverrides({})
                    }}
                  />
                  Then the next plates
                </label>
                {seqTo !== null && (
                  <div className="mt-1.5 flex items-center gap-2 text-xs text-muted">
                    up to plate
                    <select className="input h-8 w-20 py-0" value={seqTo} onChange={(e) => setSeqTo(Number(e.target.value))}>
                      {detail.plates.map((p, i) =>
                        i > plateIdx ? (
                          <option key={p.index} value={i}>
                            {p.index}
                          </option>
                        ) : null,
                      )}
                    </select>
                  </div>
                )}
                {seqTo !== null && (
                  <p className="mt-1.5 text-xs text-muted">
                    {runPlates.length} plates. After each one you get a push; clear the plate, then press Start in the app.
                  </p>
                )}
              </div>
            )}
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-3">
            {info}
            {plate && (
              <>
                <FilamentChips
                  snap={snap}
                  filaments={fils}
                  map={map}
                  overrides={overrides}
                  setOverride={(slot, t) =>
                    setOverrides((o) => {
                      const n = { ...o }
                      if (t === undefined) delete n[slot - 1]
                      else n[slot - 1] = t
                      return n
                    })
                  }
                />
                <ForecastLines rows={rows} />
              </>
            )}
            {!cloudOk && <p className="text-sm font-semibold text-warn">Log in to Bambu Cloud in Settings to print from here.</p>}
            <StartStatus snap={snap} file={file.name} since={since} />
          </div>
        </div>
      </Modal>
      {zoom && (
        <ImageZoom
          src={preview}
          title={file.name}
          info={`Printer SD card · saved ${file.date}${plate ? ` · plate ${plate.index}` : ''}`}
          onClose={() => setZoom(false)}
        />
      )}
      {summary && plate && (
        <PrintSummary
          snap={snap}
          title={file.name}
          preview={preview}
          printSec={plate.print_sec}
          filaments={fils}
          map={map}
          overrides={overrides}
          printOptions
          onStart={doStart}
          onClose={() => setSummary(false)}
        />
      )}
    </>
  )
}

// ---------------------------------------------------------------- Bambu Cloud reprint dialog

export function CloudTaskDialog({
  snap,
  task,
  onClose,
  onStarted,
}: {
  snap: Snapshot
  task: CloudTask
  onClose: () => void
  onStarted: () => void
}) {
  const dlg = useDialogs()
  const [since] = useState(() => Date.now())
  const [detail, setDetail] = useState<CloudDetail | null>(null)
  const [overrides, setOverrides] = useState<Record<number, number>>({})
  const [summary, setSummary] = useState(false)
  const [zoom, setZoom] = useState(false)

  useEffect(() => {
    api<CloudDetail>(`/api/cloud/task/${task.id}`)
      .then(setDetail)
      .catch((e) => setDetail({ ok: false, error: e.message, rows: [], ams_mapping: [] }))
  }, [task.id])

  useStartedWatcher(snap, task.title, since, () => {
    dlg.toast(true, 'Print started')
    onStarted()
  })

  const filaments: PlateFilament[] = useMemo(
    () =>
      (detail?.rows ?? []).map((r, i) => ({
        slot: i + 1,
        color: r.source_color ? `#${r.source_color.replace('#', '').slice(0, 6)}` : '',
        type: r.filament_type,
        grams: r.weight,
        meters: 0,
      })),
    [detail],
  )
  const s = snap.status!
  const auto = useMemo(() => computeAmsMap(s, filaments), [s, filaments])
  const map = applyOverrides(auto, overrides)
  const rows = forecast(s, snap.assignments, filaments, map, overrides, snap.filament_use?.by_tray ?? {})
  const cover = task.has_cover ? `/api/cloud/cover/${task.id}` : null
  const used = (detail?.ams_mapping ?? []).filter((v) => v >= 0).length
  const guessed = !!detail?.ok && (detail.ams_mapping.length === 0 || used !== detail.rows.length)
  const canPrint = !!detail?.ok && filaments.length > 0 && !startBusy(snap) && snap.status?.connected && !isPrinting(s)
  const grams = filaments.reduce((a, f) => a + f.grams, 0)

  const start = async () => {
    if (map.some((t) => t === MAP_MISSING)) {
      dlg.toast(false, 'A required filament is not loaded - pick a tray for it')
      return
    }
    if (!(await confirmShort(dlg, rows))) return
    setSummary(true)
  }
  const doStart = () => {
    api('/api/cloud/reprint', { body: { task_id: task.id, trays: map, speed_l1: snap.settings.print_speed_l1 } }).catch((e) =>
      dlg.toast(false, e.message),
    )
  }

  return (
    <>
      <Modal
        title={task.title || 'Cloud job'}
        onClose={onClose}
        wide
        footer={
          <button className="btn-accent min-w-40" disabled={!canPrint} onClick={start}>
            <Play size={18} /> PRINT AGAIN
          </button>
        }
      >
        <div className="flex flex-col gap-4 sm:flex-row">
          <div className="mx-auto w-3/5 max-w-[240px] sm:mx-0 sm:w-56 sm:max-w-none sm:shrink-0">
            <Img src={cover} className="aspect-square w-full" onClick={cover ? () => setZoom(true) : undefined} />
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-3">
            {!detail ? (
              <p className="flex items-center gap-2 text-dim">
                <Loader2 size={18} className="animate-spin text-accent" /> Loading job details from Bambu Cloud...
              </p>
            ) : !detail.ok || detail.error ? (
              <p className="text-error">{detail.error}</p>
            ) : (
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                <dt className="text-muted">Print time</dt>
                <dd className="font-semibold">~{fmtSec(task.cost_sec)}</dd>
                <dt className="text-muted">Filament</dt>
                <dd className="font-semibold">{grams > 0 ? `${grams.toFixed(1)} g` : '--'}</dd>
                <dt className="text-muted">Printed</dt>
                <dd>{task.start_time.replace('T', ' ').slice(0, 16)}</dd>
                <dt className="text-muted">Plate</dt>
                <dd>{task.plate_index}</dd>
              </dl>
            )}
            {detail?.ok && filaments.length > 0 && (
              <>
                <FilamentChips
                  snap={snap}
                  filaments={filaments}
                  map={map}
                  overrides={overrides}
                  setOverride={(slot, t) =>
                    setOverrides((o) => {
                      const n = { ...o }
                      if (t === undefined) delete n[slot - 1]
                      else n[slot - 1] = t
                      return n
                    })
                  }
                />
                <ForecastLines rows={rows} />
              </>
            )}
            <StartStatus snap={snap} file={task.title} since={since} />
          </div>
        </div>
      </Modal>
      {zoom && cover && (
        <ImageZoom src={cover} title={task.title} info={`Bambu Cloud history · printed ${task.start_time.slice(0, 16).replace('T', ' ')}`} onClose={() => setZoom(false)} />
      )}
      {summary && (
        <PrintSummary
          snap={snap}
          title={task.title}
          preview={cover}
          printSec={task.cost_sec}
          filaments={filaments}
          map={map}
          overrides={overrides}
          warning={
            guessed
              ? 'This history entry has no filament slot table - the slot order is guessed. Safer: reprint an entry that was started from Bambu Studio / Handy.'
              : undefined
          }
          printOptions={false}
          onStart={doStart}
          onClose={() => setSummary(false)}
        />
      )}
    </>
  )
}

// ---------------------------------------------------------------- reprint source (HOME zoom, finish screen)

export type ReprintSource = { kind: 'sd'; file: SdFile } | { kind: 'cloud'; task: CloudTask }

/** Reprint source of a job: the SD file of the same name, else the Bambu Cloud history (panel hasReprint). */
export function useReprintSource(snap: Snapshot, job: string, enabled: boolean): ReprintSource | null {
  const [src, setSrc] = useState<ReprintSource | null>(null)
  useEffect(() => {
    if (!enabled || !job) return
    let alive = true
    ;(async () => {
      const sd = await api<{ files: SdFile[] }>('/api/sd/files').catch(() => ({ files: [] as SdFile[] }))
      const f = findByJob(sd.files, job, (x) => x.name)
      if (f) return alive && setSrc({ kind: 'sd', file: f })
      if (!snap.cloud.logged_in) return
      const ct = await api<{ tasks: CloudTask[] }>('/api/cloud/tasks').catch(() => ({ tasks: [] as CloudTask[] }))
      const t = findByJob(ct.tasks, job, (x) => x.title)
      if (t && alive) setSrc({ kind: 'cloud', task: t })
    })()
    return () => {
      alive = false
    }
  }, [job, enabled, snap.cloud.logged_in])
  return src
}
