// Full-screen print screens (panel ui/PrintScreens.cpp), skip objects (ui/pages/SkipObjects.cpp)
// and the top-bar alert dialogs (ui/Alerts.cpp).
import { OBJ_COLORS, Paths3D, usePrintPaths } from './progress3d'
import { Check, ChevronDown, Lightbulb, LightbulbOff, ListX, Loader2, Pause, Play, RefreshCw, Square, Wrench, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, cmd, type ApiError } from './api'
import { C, isPrinting, plateClearNeeded, printerReady, untilText } from './format'
import { CloudTaskDialog, SdFileDialog, useReprintSource, type ReprintSource } from './print'
import type { PrintEnd, PrintObject, PrintObjects, Snapshot, Status } from './types'
import { Modal, Swatch, useDialogs, useLongPress } from './ui'

const COOL_BED = 35 // °C: parts come off a PEI plate much easier once it is cold
const END_SHOW_S = 12 * 3600

function lsGet(k: string): string {
  try {
    return localStorage.getItem(k) ?? ''
  } catch {
    return ''
  }
}
function lsSet(k: string, v: string) {
  try {
    localStorage.setItem(k, v)
  } catch {
    /* private mode */
  }
}

// ---------------------------------------------------------------- overlay host (in the app shell)

/** Shows the finish / pause screen when the print ends / pauses (also after a reload). */
export function PrintOverlays({ snap }: { snap: Snapshot | null }) {
  const [endSeen, setEndSeen] = useState(() => Number(lsGet('end-seen')) || 0)
  const [pauseDismissed, setPauseDismissed] = useState('')
  if (!snap) return null
  const s = snap.status
  const end = snap.last_end
  const showEnd = !!end && end.ended_at > endSeen && !isPrinting(s) && Date.now() / 1000 - end.ended_at < END_SHOW_S
  const pauseKey = s?.connected && s.state === 'PAUSE' ? `${s.job}|${snap.print_pauses}` : ''
  if (showEnd) {
    return (
      <EndScreen
        snap={snap}
        end={end!}
        onClose={() => {
          lsSet('end-seen', String(end!.ended_at))
          setEndSeen(end!.ended_at)
        }}
      />
    )
  }
  if (pauseKey && pauseKey !== pauseDismissed && s) {
    return <PauseScreen snap={snap} s={s} onClose={() => setPauseDismissed(pauseKey)} />
  }
  return null
}

function ScreenFrame({ children }: { children: ReactNode }) {
  return (
    <div className="safe-top safe-bottom safe-x fixed inset-0 z-40 overflow-y-auto bg-bg">
      <div className="mx-auto flex min-h-full max-w-6xl flex-col gap-5 p-4 md:p-8">{children}</div>
    </div>
  )
}

function BigCover({ snap, color, fallback }: { snap: Snapshot; color: string; fallback: ReactNode }) {
  const [failed, setFailed] = useState(false)
  return (
    <div
      className="mx-auto flex aspect-square w-[min(72vw,36vh)] items-center justify-center overflow-hidden rounded-2xl border-b-[6px] bg-card md:w-full md:max-w-[420px]"
      style={{ borderColor: color, boxShadow: `0 10px 40px -18px ${color}` }}
    >
      {snap.cover_job && !failed ? (
        <img
          src={`/api/cover?v=${snap.cover_version}`}
          alt=""
          className="h-full w-full object-contain p-2"
          onError={() => setFailed(true)}
        />
      ) : (
        <div style={{ color }}>{fallback}</div>
      )}
    </div>
  )
}

function Title({ text, color }: { text: string; color: string }) {
  return (
    <div className="text-5xl font-extrabold tracking-wide md:text-6xl" style={{ color, textShadow: `0 0 24px ${color}66` }}>
      {text}
    </div>
  )
}

// ---------------------------------------------------------------- print finished / failed

function fmtHm(min: number): string {
  return `${Math.floor(min / 60)}h ${String(min % 60).padStart(2, '0')}m`
}

function EndScreen({ snap, end, onClose }: { snap: Snapshot; end: PrintEnd; onClose: () => void }) {
  const s = snap.status
  const accent = end.failed ? C.error : C.accent
  const src = useReprintSource(snap, end.job, true)
  const [reprint, setReprint] = useState<ReprintSource | null>(null)

  const min = end.duration_min >= 0 ? end.duration_min : end.archive_sec && end.archive_sec > 0 ? Math.round(end.archive_sec / 60) : -1
  const rows: [string, string][] = []
  if (min >= 0) rows.push(['Time', fmtHm(min)])
  if (end.layers > 0)
    rows.push(['Layers', end.failed && end.layer_reached > 0 ? `stopped at ${end.layer_reached} / ${end.layers}` : String(end.layers)])
  if (end.avg_layer_sec > 0) rows.push(['Avg layer', `${end.avg_layer_sec} s`])
  rows.push([
    'Ended',
    new Date(end.ended_at * 1000).toLocaleTimeString('en-GB', {
      hour: '2-digit',
      minute: '2-digit',
    }),
  ])

  const fil = end.filament ?? []
  const total = fil.reduce((a, f) => a + f.grams, 0)
  const maint = snap.maintenance?.items.length ? snap.maintenance.items.reduce((a, b) => (b.until < a.until ? b : a)) : null

  const clearPlate = async () => {
    if ((await cmd('clear_plate')).ok) onClose()
  }

  return (
    <ScreenFrame>
      <div className="grid flex-1 items-center gap-6 md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] md:gap-10">
        <div className="flex flex-col gap-3">
          <BigCover snap={snap} color={accent} fallback={end.failed ? <X size={96} /> : <Check size={96} />} />
          {s?.connected && <CoolLine bed={s.bed} />}
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <Title text={end.failed ? 'FAILED' : 'FINISHED'} color={accent} />
          <div className="line-clamp-2 text-xl font-bold [overflow-wrap:anywhere] md:text-2xl">{end.job || 'Print'}</div>
          <dl className="grid grid-cols-[auto_1fr] gap-x-5 gap-y-1.5 text-lg">
            {rows.map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted">{k}</dt>
                <dd className="font-semibold tabular-nums">{v}</dd>
              </div>
            ))}
          </dl>
          <div className="flex flex-wrap items-center gap-2 pt-1">
            {end.filament_state === 'loading' && (
              <span className="flex items-center gap-2 text-sm text-muted">
                <Loader2 size={14} className="animate-spin" /> Filament used: loading...
              </span>
            )}
            {fil.map((f, i) => (
              <span key={i} className="flex items-center gap-2 rounded-full bg-card py-1.5 pr-3 pl-1.5 text-sm font-semibold">
                <Swatch color={f.color} size={22} />
                {f.type || '?'} {f.grams.toFixed(1)} g{f.where && <span className="text-muted">{f.where}</span>}
              </span>
            ))}
            {fil.length >= 2 && total > 0 && <span className="text-sm text-muted">Total {total.toFixed(1)} g</span>}
          </div>
          {maint && (
            <div className="flex items-start gap-2 text-sm font-semibold" style={{ color: maint.due ? C.error : C.warn }}>
              <Wrench size={16} className="mt-0.5 shrink-0" />
              <span>
                Next maintenance: {maint.name} - {untilText(maint)}
              </span>
            </div>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:flex sm:flex-wrap sm:justify-end md:[&>button]:text-base">
        <EndLightButton snap={snap} />
        {plateClearNeeded(snap) && (
          <button className="btn-warn h-14 sm:px-6" onClick={clearPlate}>
            <Check size={20} /> Plate cleared
          </button>
        )}
        {src && s?.connected && (
          <button className="btn h-14 sm:px-6" onClick={() => setReprint(src)}>
            <RefreshCw size={20} /> Reprint
          </button>
        )}
        <button className="btn h-14 sm:px-8" onClick={onClose}>
          Close
        </button>
      </div>
      {reprint?.kind === 'sd' && (
        <SdFileDialog
          snap={snap}
          file={reprint.file}
          onClose={() => setReprint(null)}
          onStarted={() => {
            setReprint(null)
            onClose()
          }}
        />
      )}
      {reprint?.kind === 'cloud' && (
        <CloudTaskDialog
          snap={snap}
          task={reprint.task}
          onClose={() => setReprint(null)}
          onStarted={() => {
            setReprint(null)
            onClose()
          }}
        />
      )}
    </ScreenFrame>
  )
}

function CoolLine({ bed }: { bed: number }) {
  if (bed < COOL_BED)
    return (
      <div className="text-center font-semibold">
        <span className="text-accent">✓ Ready to remove</span> <span className="text-muted">bed {Math.round(bed)}°C</span>
      </div>
    )
  return (
    <div className="flex items-center justify-center gap-1 font-semibold">
      <ChevronDown size={18} style={{ color: C.cool }} />
      Bed {Math.round(bed)}°C, cooling to {COOL_BED}°C...
    </div>
  )
}

/** Remaining seconds of the backend's "light off after the print" timer, counting down locally. */
function useEndLightLeft(snap: Snapshot): number {
  const deadline = useRef(0)
  const [, setTick] = useState(0)
  useEffect(() => {
    deadline.current = snap.end_light_remaining > 0 ? Date.now() / 1000 + snap.end_light_remaining : 0
  }, [snap.version, snap.end_light_remaining])
  useEffect(() => {
    const t = window.setInterval(() => setTick((x) => x + 1), 1000)
    return () => window.clearInterval(t)
  }, [])
  return deadline.current ? Math.max(0, Math.ceil(deadline.current - Date.now() / 1000)) : 0
}

// Off: on, and off again in 2 min. On with the countdown running: 2 min again.
// On without a countdown: off. Hold: off now.
function EndLightButton({ snap }: { snap: Snapshot }) {
  const on = !!snap.status?.light
  const left = useEndLightLeft(snap)
  const ready = printerReady(snap)
  const setLight = async (v: boolean) => {
    if ((await cmd('light', { on: v })).ok) await cmd('restart_end_light', { on: v })
  }
  const press = useLongPress(
    () => {
      if (on) void setLight(false)
    },
    () => {
      if (!ready) return
      if (!on) void setLight(true)
      else if (left > 0) void cmd('restart_end_light', { on: true })
      else void setLight(false)
    },
  )
  return (
    <button {...press} disabled={!ready} className={`${on ? 'btn-warn' : 'btn'} h-14 flex-col gap-0 leading-tight sm:min-w-52 sm:px-6`}>
      <span className="flex items-center gap-2">
        {on ? <Lightbulb size={20} /> : <LightbulbOff size={20} />}
        {!on ? 'Light off' : left > 0 ? `Off in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}` : 'Light on'}
      </span>
      {on && left > 0 && <span className="text-[11px] font-semibold opacity-75">Tap: +2 min, hold: off</span>}
    </button>
  )
}

// ---------------------------------------------------------------- paused print

const PAUSE_RULES: [string[], string][] = [
  [['run out', 'ran out', 'runout'], 'Load new filament (AMS or external spool), then press Resume.'],
  [['used up', 'depleted'], 'Load new filament (AMS or external spool), then press Resume.'],
  [['tangle', 'stuck', 'feed'], 'Check the AMS / spool for tangled or stuck filament, free it, then press Resume.'],
  [['clog', 'extrusion', 'extruder'], 'The nozzle may be clogged: heat it, push / pull filament through, then press Resume.'],
  [
    ['spaghetti', 'failure', 'detached'],
    'The print may have failed or come loose: check the plate. Stop if it is ruined, otherwise Resume.',
  ],
  [['first layer', 'lidar', 'inspection'], 'First-layer problem: check the plate and its surface before you Resume.'],
  [['door', 'cover', 'lid'], 'Close the printer door / top cover, then press Resume.'],
  [['temperature', 'heat', 'thermal'], 'A heater reported a problem: check the temperatures on HOME, then Resume.'],
  [['plate', 'build plate', 'bed'], 'Check that the build plate is placed correctly, then press Resume.'],
  [['collision', 'hit', 'blocked'], 'The toolhead hit something: check for loose parts on the plate, then Resume.'],
]

/** What to do, from the printer's own messages (keywords of common Bambu HMS texts). */
export function pauseAdvice(s: Status): string {
  for (const h of s.hms) {
    const t = h.text.toLowerCase()
    for (const [keys, advice] of PAUSE_RULES) if (keys.some((k) => t.includes(k))) return advice
  }
  return s.hms.length
    ? 'Read the printer message above, fix the cause, then press Resume.'
    : 'The print was paused (by you, the slicer or the printer). Press Resume to continue.'
}

function PauseScreen({ snap, s, onClose }: { snap: Snapshot; s: Status; onClose: () => void }) {
  const dlg = useDialogs()
  const [skip, setSkip] = useState(false)
  const ready = printerReady(snap)
  const stop = async () => {
    const ok = await dlg.confirm({
      title: 'Stop print?',
      text: 'The print is cancelled and cannot be resumed.',
      ok: 'Stop',
      tone: 'danger',
    })
    if (ok) await cmd('stop')
  }
  const resume = async () => {
    if ((await cmd('resume')).ok) onClose()
  }
  return (
    <ScreenFrame>
      <div className="flex justify-end">
        <button className="btn h-12 w-12 p-0" onClick={onClose} title="Close (the print stays paused)">
          <X size={24} />
        </button>
      </div>
      <div className="grid flex-1 items-center gap-6 md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] md:gap-10">
        <BigCover snap={snap} color={C.warn} fallback={<Pause size={96} />} />
        <div className="flex min-w-0 flex-col gap-3">
          <Title text="PAUSED" color={C.warn} />
          <div className="line-clamp-2 text-xl font-bold [overflow-wrap:anywhere] md:text-2xl">{s.job || 'Print'}</div>
          <div className="text-lg font-semibold text-muted tabular-nums">
            {Math.floor(s.progress)}%{s.total_layers > 0 && `   ·   Layer ${s.layer} / ${s.total_layers}`}
          </div>
          {s.hms.slice(0, 2).map((h, i) => (
            <div
              key={i}
              className="rounded-xl border-l-4 bg-card px-4 py-2.5 text-sm"
              style={{ borderColor: h.severity <= 1 ? C.error : C.warn }}
            >
              {h.text || 'Printer message'}
            </div>
          ))}
          <div className="text-lg leading-snug">{pauseAdvice(s)}</div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:flex sm:justify-end md:[&>button]:text-base">
        {snap.objects_count > 1 && (
          <button className="btn col-span-2 h-14 sm:px-6" disabled={!ready} onClick={() => setSkip(true)}>
            <ListX size={20} /> Skip objects
          </button>
        )}
        <button className="btn-danger h-14 sm:px-6" disabled={!ready} onClick={stop}>
          <Square size={20} /> Stop
        </button>
        <button className="btn-accent h-14 text-lg sm:px-10" disabled={!ready} onClick={resume}>
          <Play size={22} /> Resume
        </button>
      </div>
      {skip && <SkipDialog snap={snap} onClose={() => setSkip(false)} />}
    </ScreenFrame>
  )
}

// ---------------------------------------------------------------- skip objects

const PICK = 360
const SHAPE_SELECTED = [0xff, 0x52, 0x52]
const SHAPE_SKIPPED = [0x45, 0x4b, 0x57]

/** Object index per pixel of the pick map (-1 none) and each object's centre. */
interface PickMap {
  idx: Int16Array
  centers: ({ x: number; y: number } | null)[]
}

function usePickMap(has: boolean, version: number, count: number): PickMap | null {
  const [map, setMap] = useState<PickMap | null>(null)
  useEffect(() => {
    if (!has || !count) {
      setMap(null)
      return
    }
    let alive = true
    const img = new Image()
    img.onload = () => {
      if (!alive) return
      const c = document.createElement('canvas')
      c.width = PICK
      c.height = PICK
      const ctx = c.getContext('2d', { willReadFrequently: true })
      if (!ctx) return
      ctx.drawImage(img, 0, 0, PICK, PICK)
      const px = ctx.getImageData(0, 0, PICK, PICK).data
      const idx = new Int16Array(PICK * PICK)
      const sx = new Float64Array(count)
      const sy = new Float64Array(count)
      const n = new Float64Array(count)
      for (let p = 0; p < PICK * PICK; p++) {
        const k = px[p * 4] - 1 // grayscale: value = index + 1
        idx[p] = k >= 0 && k < count ? k : -1
        if (idx[p] >= 0) {
          sx[k] += p % PICK
          sy[k] += Math.floor(p / PICK)
          n[k]++
        }
      }
      const centers = Array.from({ length: count }, (_, k) => (n[k] ? { x: sx[k] / n[k], y: sy[k] / n[k] } : null))
      setMap({ idx, centers })
    }
    img.src = `/api/print/pick?v=${version}`
    return () => {
      alive = false
    }
  }, [has, version, count])
  return map
}

function ShapeCanvas({
  map,
  items,
  selected,
  onToggle,
}: {
  map: PickMap
  items: PrintObject[]
  selected: Set<number>
  onToggle: (i: number) => void
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const ctx = ref.current?.getContext('2d')
    if (!ctx) return
    const img = ctx.createImageData(PICK, PICK)
    // same colour per object as in the list and the 3D view
    const own = (i: number) => {
      const h = OBJ_COLORS[i % OBJ_COLORS.length].slice(1)
      return [0, 2, 4].map((k) => Math.round(parseInt(h.slice(k, k + 2), 16) * 0.7))
    }
    const cols = items.map((o, i) => (o.skipped ? SHAPE_SKIPPED : selected.has(o.id) ? SHAPE_SELECTED : own(i)))
    for (let p = 0; p < PICK * PICK; p++) {
      const k = map.idx[p]
      if (k < 0 || !cols[k]) continue
      img.data.set(cols[k], p * 4)
      img.data[p * 4 + 3] = 255
    }
    ctx.putImageData(img, 0, 0)
  }, [map, items, selected])
  const click = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    const scale = PICK / r.width
    const x = (e.clientX - r.left) * scale
    const y = (e.clientY - r.top) * scale
    const tol = Math.max(18, 22 * scale) // finger tolerance around small objects
    let best = -1
    let bestD = tol * tol
    for (let dy = -tol; dy <= tol; dy += 2)
      for (let dx = -tol; dx <= tol; dx += 2) {
        const sx = Math.round(x + dx)
        const sy = Math.round(y + dy)
        if (sx < 0 || sy < 0 || sx >= PICK || sy >= PICK) continue
        const k = map.idx[sy * PICK + sx]
        if (k < 0) continue
        const d = dx * dx + dy * dy
        if (d < bestD) {
          bestD = d
          best = k
        }
      }
    if (best >= 0) onToggle(best)
  }
  return (
    <>
      <canvas ref={ref} width={PICK} height={PICK} onClick={click} className="absolute inset-0 h-full w-full cursor-pointer" />
      {items.map((o, i) => {
        const c = map.centers[i]
        if (!c) return null
        const sel = selected.has(o.id)
        return (
          <span
            key={o.id}
            className="pointer-events-none absolute flex h-6 min-w-7 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full px-1.5 text-xs font-bold"
            style={{
              left: `${(c.x / PICK) * 100}%`,
              top: `${(c.y / PICK) * 100}%`,
              background: o.skipped ? '#2A2F3A' : sel ? '#7A1F1F' : '#1D2129',
              color: o.skipped ? C.muted : C.text,
            }}
          >
            {o.skipped ? '✕' : i + 1}
          </span>
        )
      })}
    </>
  )
}

/** Without the pick image: numbered markers at the objects' x/y (seen from above, front at the bottom). */
function MarkerMap({
  items,
  bbox,
  selected,
  onToggle,
}: {
  items: PrintObject[]
  bbox: PrintObjects['bbox']
  selected: Set<number>
  onToggle: (i: number) => void
}) {
  const pos = useMemo(() => {
    let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity]
    for (const o of items) {
      if (o.x < 0 || o.y < 0) continue
      x0 = Math.min(x0, o.x)
      y0 = Math.min(y0, o.y)
      x1 = Math.max(x1, o.x)
      y1 = Math.max(y1, o.y)
    }
    if (bbox) {
      x0 = Math.min(x0, bbox[0])
      y0 = Math.min(y0, bbox[1])
      x1 = Math.max(x1, bbox[2])
      y1 = Math.max(y1, bbox[3])
    }
    if (x1 < x0) [x0, y0, x1, y1] = [0, 0, 256, 256]
    const span = Math.max(Math.max(x1 - x0, y1 - y0) + 24, 40)
    const cx = (x0 + x1) / 2
    const cy = (y0 + y1) / 2
    const inner = 0.86 // markers stay inside
    return items.map((o, i) =>
      o.x >= 0 && o.y >= 0
        ? {
            left: 50 + ((o.x - cx) / span) * 100 * inner,
            top: 50 - ((o.y - cy) / span) * 100 * inner,
          }
        : { left: 8 + (i % 7) * 13, top: 8 + Math.floor(i / 7) * 13 },
    )
  }, [items, bbox])
  return (
    <>
      <span className="absolute bottom-1 left-1/2 -translate-x-1/2 text-xs font-bold tracking-widest text-muted">FRONT</span>
      {items.map((o, i) => {
        const sel = selected.has(o.id)
        return (
          <button
            key={o.id}
            disabled={o.skipped}
            onClick={() => onToggle(i)}
            className="absolute flex h-11 w-11 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border-2 border-border text-sm font-bold transition"
            style={{
              left: `${pos[i].left}%`,
              top: `${pos[i].top}%`,
              background: o.skipped ? '#3A3F4A' : sel ? C.error : C.card2,
              color: o.skipped ? C.muted : C.text,
            }}
          >
            {o.skipped ? '✕' : i + 1}
          </button>
        )
      })}
    </>
  )
}

export function SkipDialog({ snap, onClose }: { snap: Snapshot; onClose: () => void }) {
  const dlg = useDialogs()
  const [data, setData] = useState<PrintObjects | null>(null)
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [sending, setSending] = useState(false)

  useEffect(() => {
    api<PrintObjects>('/api/print/objects?refresh=1')
      .then(setData)
      .catch((e: ApiError) => dlg.toast(false, e.message))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  useEffect(() => {
    if (!data) return
    api<PrintObjects>('/api/print/objects')
      .then(setData)
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [snap.objects_version])

  const items = data?.items ?? []
  const map = usePickMap(!!data?.has_pick, data?.version ?? 0, items.length)
  const paths = usePrintPaths(snap.status?.job ?? '')
  const [view, setView] = useState<'3d' | 'top'>('3d')
  const show3d = view === '3d'
  // Keep the choice only for objects that are still there and not skipped yet.
  const live = new Set([...selected].filter((id) => items.some((o) => o.id === id && !o.skipped)))
  const toggleId = (id: number) => {
    const i = items.findIndex((o) => o.id === id)
    if (i >= 0) toggle(i)
  }
  const toggle = (i: number) => {
    const o = items[i]
    if (!o || o.skipped) return
    const n = new Set(live)
    if (n.has(o.id)) n.delete(o.id)
    else n.add(o.id)
    setSelected(n)
  }
  const skippedCount = items.filter((o) => o.skipped).length
  const skip = async () => {
    const ids = [...live].slice(0, 24)
    if (!ids.length) return
    const ok = await dlg.confirm({
      title: 'Skip objects?',
      text: `${ids.length} object(s) will not be printed any further. The rest of the print continues. This cannot be undone.`,
      ok: 'Skip',
      tone: 'danger',
    })
    if (!ok) return
    setSending(true)
    try {
      const r = await api<{ ok: boolean; message: string }>('/api/print/skip', {
        body: { ids },
      })
      if (r.ok) setSelected(new Set())
      else dlg.toast(false, r.message)
    } catch (e) {
      dlg.toast(false, (e as ApiError).message)
    } finally {
      setSending(false)
    }
  }

  let empty = ''
  if (!data || (!data.loaded && data.loading)) empty = 'Loading objects...'
  else if (!data.loaded) empty = data.error || 'Objects not available'
  else if (!items.length) empty = 'No objects in this print'

  return (
    <Modal
      title={
        <span className="flex items-center gap-2">
          <ListX size={20} /> Skip objects
        </span>
      }
      onClose={onClose}
      wide
      footer={
        <button className="btn-danger w-full sm:w-auto" disabled={!live.size || sending || !isPrinting(snap.status)} onClick={skip}>
          {sending ? <Loader2 size={18} className="animate-spin" /> : <X size={18} />}
          {live.size ? `Skip ${live.size} object(s)` : 'Select objects to skip'}
        </button>
      }
    >
      <div className="flex flex-col gap-4 sm:flex-row">
        <div className="mx-auto flex w-full max-w-[420px] shrink-0 flex-col gap-2 sm:w-[380px]">
          {/* 2D / 3D switch above the picture (always visible) */}
          <div className="flex gap-0.5 self-start rounded-lg bg-card p-0.5">
            {(['3d', 'top'] as const).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`rounded-md px-3 py-1.5 text-xs font-bold ${view === v ? 'bg-card2 text-accent' : 'text-muted hover:text-text'}`}
              >
                {v === '3d' ? '3D' : '2D'}
              </button>
            ))}
          </div>
          <div className="relative aspect-square w-full overflow-hidden rounded-xl bg-card">
            {empty ? (
              <div className="flex h-full items-center justify-center p-6 text-center text-muted">
                {data?.loading && <Loader2 size={18} className="mr-2 animate-spin" />}
                {empty}
              </div>
            ) : view === '3d' ? (
              <Paths3D
                data={paths.data}
                msg={paths.msg}
                live={snap.status?.layer ?? 0}
                job={snap.status?.job ?? ''}
                totalHint={snap.status?.total_layers ?? 0}
                objects={items.map((o, i) => ({
                  id: o.id,
                  color: OBJ_COLORS[i % OBJ_COLORS.length],
                  skipped: o.skipped,
                  selected: live.has(o.id),
                }))}
                onPick={toggleId}
              />
            ) : map ? (
              <ShapeCanvas map={map} items={items} selected={live} onToggle={toggle} />
            ) : (
              <MarkerMap items={items} bbox={data!.bbox} selected={live} onToggle={toggle} />
            )}
          </div>
        </div>
        <div className="flex min-w-0 flex-1 flex-col gap-3 text-sm text-dim">
          <p>
            Tap the objects that failed (e.g. came loose) - on the picture or in the list. They turn red.
            {show3d && <span className="text-muted"> Drag sideways to turn the view.</span>}
          </p>
          {data?.loaded && (
            <p className="text-muted">
              {items.length} object(s)
              {skippedCount ? `, ${skippedCount} already skipped` : ''}
            </p>
          )}
          {items.length > 0 && (
            <ul className="flex max-h-72 flex-col gap-1.5 overflow-y-auto pr-1">
              {items.map((o, i) => {
                const sel = live.has(o.id)
                return (
                  <li key={o.id}>
                    <button
                      disabled={o.skipped}
                      onClick={() => toggle(i)}
                      className={`flex w-full items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition ${
                        o.skipped
                          ? 'border-transparent bg-card opacity-45'
                          : sel
                            ? 'border-error/70 bg-error/15 text-error'
                            : 'border-border bg-card hover:border-accent/50'
                      }`}
                    >
                      <span
                        className="flex h-6 min-w-6 shrink-0 items-center justify-center rounded-full border-2 px-1 text-xs font-bold text-white"
                        style={{
                          background: '#0b0e13',
                          borderColor: o.skipped ? '#555' : sel ? '#FF4D5E' : OBJ_COLORS[i % OBJ_COLORS.length],
                          boxShadow: o.skipped ? undefined : `0 0 8px ${sel ? '#FF4D5E' : OBJ_COLORS[i % OBJ_COLORS.length]}`,
                        }}
                      >
                        {i + 1}
                      </span>
                      <span className="min-w-0 flex-1 truncate font-semibold">{o.name || `Object ${i + 1}`}</span>
                      <span className="text-xs text-muted">{o.skipped ? 'skipped' : sel ? 'will skip' : ''}</span>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
          {!show3d && <p className="text-xs text-muted">Seen from above, front of the bed at the bottom.</p>}
        </div>
      </div>
    </Modal>
  )
}

/** HOME / focus view Skip button: one object only → a tap explains, holding opens it anyway. */
export function useSkipButton(snap: Snapshot, open: () => void) {
  const dlg = useDialogs()
  return useLongPress(open, () => {
    if (snap.objects_count === 1) dlg.toast(false, 'Only one object in this print - hold the button to open it anyway')
    else open()
  })
}

// ---------------------------------------------------------------- top-bar alert dialogs

export function LowFilamentDialog({ snap, onClose }: { snap: Snapshot; onClose: () => void }) {
  const { low, runout } = snap.alerts
  return (
    <Modal title="Low filament" onClose={onClose}>
      <div className="flex flex-col gap-2">
        {runout && (
          <div className="rounded-xl border-l-4 bg-card px-4 py-3" style={{ borderColor: runout.short ? C.error : C.accent }}>
            <div className={runout.short ? 'font-semibold text-error' : ''}>{runout.text}</div>
            <div className="mt-1 text-xs text-muted">Estimate: the print's filament vs the spool as it was at the start.</div>
          </div>
        )}
        {low.map((l) => (
          <div
            key={l.id}
            className="flex items-center gap-3 rounded-xl border-l-4 bg-card px-4 py-3"
            style={{ borderColor: l.active ? C.error : C.warn }}
          >
            <Swatch color={l.color} size={34} />
            <div className="min-w-0 flex-1">
              <div className="truncate font-semibold">
                {l.label}{' '}
                <span className="text-dim">
                  {l.type} {l.sub_brands}
                </span>
                {l.active && <span className="text-error"> (printing from it)</span>}
              </div>
              <div className="text-sm" style={{ color: l.active ? C.error : C.warn }}>
                {[l.remain >= 0 && `AMS estimate ${l.remain} %`, l.spool_left !== null && `spool ${l.spool_left} g left`]
                  .filter(Boolean)
                  .join('   ')}
              </div>
            </div>
          </div>
        ))}
        {!low.length && !runout?.short && <p className="text-muted">All loaded trays have enough filament.</p>}
      </div>
    </Modal>
  )
}

export function HumidityDialog({ snap, onClose }: { snap: Snapshot; onClose: () => void }) {
  const units = snap.status?.ams ?? []
  return (
    <Modal
      title="AMS humidity"
      onClose={onClose}
      footer={
        <button
          className="btn"
          onClick={() => {
            onClose()
            location.hash = '#/ams'
          }}
        >
          Open AMS page
        </button>
      }
    >
      <div className="flex flex-col gap-2">
        {units.map((u) => {
          const high = u.humidity > 40
          return (
            <div
              key={u.id}
              className="flex items-center gap-6 rounded-xl border-l-4 bg-card px-4 py-3 text-lg"
              style={{ borderColor: high ? C.info : C.accent }}
            >
              <span className="font-semibold">
                {u.is_ht || u.id >= 128 ? `AMS-HT ${u.id - 127}` : `AMS ${String.fromCharCode(65 + u.id)}`}
              </span>
              <span className={high ? 'font-bold text-info' : ''}>{u.humidity} % humidity</span>
              {u.temp !== null && <span className="text-muted">{Math.round(u.temp)}°C</span>}
            </div>
          )
        })}
        <p className="text-sm text-muted">Above 40 % for 10 minutes: dry the filament or replace the desiccant.</p>
      </div>
    </Modal>
  )
}
