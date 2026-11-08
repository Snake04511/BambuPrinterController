// HOME: same functions as the panel's HomePage.cpp, with a layout per screen size:
//  - phone: compact hero (cover, job, progress bar), temps, speed / filament, one button row; fits one screen
//  - tablet / desktop: fills the viewport height; progress ring + details left, big cover + temps + controls right
import { Film, Image as ImageIcon, Lightbulb, LightbulbOff, ListX, Pause, Play, Settings2, Square, TriangleAlert, Zap } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { api, cmd } from '../api'
import {
  activeTray,
  C,
  clock,
  formatDuration,
  formatEta,
  hasJob,
  isPrinting,
  online,
  plateClearNeeded,
  printerReady,
  SPEED_NAMES,
  stateColor,
  stateText,
  tempColor,
} from '../format'
import type { Snapshot, Status } from '../types'
import { CloudTaskDialog, SdFileDialog, useReprintSource, type ReprintSource } from '../print'
import { SkipDialog, useSkipButton } from '../PrintScreens'
import { MediaView, useMediaMode } from '../media'
import { TimelapsePlayer } from '../timelapse'
import { DeskHome, fmtLayer } from './HomeDesk'
import { Modal, Swatch, useDialogs, type Choice } from '../ui'

export default function Home({ snap, goSettings, goPage }: { snap: Snapshot | null; goSettings: () => void; goPage: (p: 'ams' | 'control') => void }) {
  const [focus, setFocus] = useState(false)
  const [zoom, setZoom] = useState(false)
  const [reprint, setReprint] = useState<ReprintSource | null>(null)
  if (!snap || !online(snap)) {
    return (
      <div className="flex h-full min-h-[60vh] flex-col items-center justify-center gap-4 p-6 text-center">
        <TriangleAlert size={56} className="text-warn" />
        <div className="text-xl font-bold">Not connected</div>
        <div className="max-w-md text-muted">{snap ? snap.link_text || 'Starting...' : 'Connecting to the server...'}</div>
        <button className="btn-accent" onClick={goSettings}>
          <Settings2 size={18} /> Open Settings
        </button>
      </div>
    )
  }
  const s = snap.status!
  const openZoom = () => snap.cover_job && setZoom(true)
  return (
    <>
      {/* phone (portrait or landscape) */}
      <div className="mx-auto flex min-h-full max-w-xl flex-col gap-3 p-3 wide:hidden">
        <CompactHero snap={snap} s={s} onZoom={openZoom} onFocus={() => setFocus(true)} />
        <TempTiles snap={snap} s={s} compact />
        <InfoChips snap={snap} s={s} />
        <Controls snap={snap} s={s} compact />
      </div>

      {/* tablet / desktop: fill the height when there is enough of it */}
      {/* landscape: status card left (3 rows), cover / temps / controls right.
          portrait: status + cover side by side, temps and controls full width below. */}
      <div className="mx-auto hidden h-full max-w-[1700px] grid-cols-[minmax(0,5fr)_minmax(0,6fr)] grid-rows-[minmax(0,1fr)_auto_auto] gap-4 p-4 wide:grid desk:hidden! xl:grid-cols-[minmax(0,4fr)_minmax(0,5fr)] portrait:grid-cols-1 portrait:grid-rows-[auto_minmax(0,1fr)_auto_auto]">
        <StatusCard snap={snap} s={s} onFocus={() => setFocus(true)} />
        <CoverCard snap={snap} s={s} onZoom={openZoom} />
        <div className="landscape:col-start-2">
          <TempTiles snap={snap} s={s} />
        </div>
        <div className="landscape:col-start-2">
          <Controls snap={snap} s={s} />
        </div>
      </div>

      {/* laptop / desktop: web dashboard (camera, progress, controls, move, AMS on one screen) */}
      <DeskHome snap={snap} s={s} onZoom={openZoom} onFocus={() => setFocus(true)} goPage={goPage} />

      {zoom && (
        <ZoomCover
          snap={snap}
          s={s}
          onClose={() => setZoom(false)}
          onReprint={(src) => {
            setZoom(false)
            setReprint(src)
          }}
        />
      )}
      {reprint?.kind === 'sd' && (
        <SdFileDialog snap={snap} file={reprint.file} onClose={() => setReprint(null)} onStarted={() => setReprint(null)} />
      )}
      {reprint?.kind === 'cloud' && (
        <CloudTaskDialog snap={snap} task={reprint.task} onClose={() => setReprint(null)} onStarted={() => setReprint(null)} />
      )}
      {focus && <FocusView snap={snap} onClose={() => setFocus(false)} />}
    </>
  )
}

// ---------------------------------------------------------------- shared helpers

export function jobTitle(s: Status): string {
  return hasJob(s) && s.job ? s.job : s.connected ? 'No active print' : 'Printer offline'
}

export function progressColor(s: Status): string {
  return s.state === 'PAUSE' ? C.warn : stateColor(s)
}

export function CoverImage({ snap, className, onClick }: { snap: Snapshot; className?: string; onClick?: () => void }) {
  if (!snap.cover_job) {
    return (
      <div className={`flex items-center justify-center rounded-xl bg-panel text-card2 ${className ?? ''}`}>
        <ImageIcon className="h-1/3 w-1/3" />
      </div>
    )
  }
  return (
    <img
      src={`/api/cover?v=${snap.cover_version}`}
      alt=""
      onClick={onClick}
      className={`rounded-xl bg-panel object-contain ${onClick ? 'cursor-zoom-in' : ''} ${className ?? ''}`}
    />
  )
}

export function useSpeedPicker(snap: Snapshot, s: Status) {
  const dlg = useDialogs()
  const printing = isPrinting(s)
  const pendingL1 = printing && snap.l1_speed > 0
  return async () => {
    if (!printerReady(snap)) {
      dlg.toast(false, 'Printer is not reachable')
      return
    }
    const sel = pendingL1 ? -snap.l1_speed : s.speed_level
    const choices: Choice<number>[] = [1, 2, 3, 4].map((m) => ({ label: SPEED_NAMES[m], value: m, selected: sel === m }))
    if (printing && s.layer < 2) {
      for (const m of [2, 3, 4])
        choices.push({
          label: (
            <span className="flex items-center gap-2">
              <Zap size={16} className="text-warn" /> {SPEED_NAMES[m]} after the first layer
            </span>
          ),
          value: -m,
          selected: sel === -m,
        })
      if (pendingL1) choices.push({ label: '✕ Cancel the switch after layer 1', value: 0 })
    }
    const v = await dlg.choose({ title: 'Print speed', choices })
    if (v === null) return
    if (v <= 0) await cmd('set_first_layer_speed', { level: -v })
    else await cmd('speed', { mode: v })
  }
}

export function SpeedValue({ snap, s }: { snap: Snapshot; s: Status }) {
  const pendingL1 = isPrinting(s) && snap.l1_speed > 0
  return (
    <>
      {SPEED_NAMES[s.speed_level] ?? '--'}
      {pendingL1 && (
        <span className="ml-2 text-warn">
          <Zap size={14} className="inline -translate-y-px" /> {SPEED_NAMES[snap.l1_speed]}
        </span>
      )}
    </>
  )
}

export function FilamentValue({ s }: { s: Status }) {
  const tray = activeTray(s)
  if (!tray || !tray.has_filament) return <span className="text-muted">No filament loaded</span>
  return (
    <span className="flex min-w-0 items-center gap-2">
      <Swatch color={tray.color} size={16} />
      <span className="truncate">
        {tray.sub_brands || tray.type}
        {tray.remain >= 0 && <span className="text-muted"> · {tray.remain}%</span>}
      </span>
    </span>
  )
}

function Caption({ children }: { children: ReactNode }) {
  return <div className="text-[11px] font-bold tracking-wider text-muted uppercase">{children}</div>
}

// ---------------------------------------------------------------- phone

function CompactHero({ snap, s, onZoom, onFocus }: { snap: Snapshot; s: Status; onZoom: () => void; onFocus: () => void }) {
  const printing = isPrinting(s)
  const job = hasJob(s)
  const pct = job ? s.progress : 0
  const eta = printing ? formatEta(s.remaining_min) : null
  const color = progressColor(s)
  return (
    <div className="card flex flex-1 flex-col p-3">
      <div className="relative mb-3 min-h-[120px] flex-1">
        <div className="absolute inset-0">
          <MediaView snap={snap} className="h-full w-full" onClick={onZoom} compactSwitch />
        </div>
      </div>
      <div className="flex gap-3">
        <button className="flex min-w-0 flex-1 flex-col text-left" onClick={onFocus}>
          <div className="line-clamp-2 leading-snug font-bold [overflow-wrap:anywhere]">{jobTitle(s)}</div>
          <div className="mt-auto flex items-end justify-between gap-2 pt-1">
            <span className="text-xs font-semibold text-muted">
              {job && s.total_layers > 0 ? `Layer ${s.layer} / ${s.total_layers}` : stateText(s)}
              {printing && snap.layer_stats.avg_sec > 0 && ` · ${fmtLayer(snap.layer_stats.avg_sec)}/layer`}
            </span>
            <span className="text-3xl leading-none font-extrabold tabular-nums" style={{ color: job ? C.text : C.muted }}>
              {Math.floor(pct)}%
            </span>
          </div>
        </button>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-card2">
        <div
          className="h-full rounded-full transition-[width] duration-700"
          style={{ width: `${pct}%`, background: color, boxShadow: `0 0 10px ${color}` }}
        />
      </div>
      {printing && (
        <div className="mt-2 flex items-center justify-between text-sm">
          <span className="font-semibold">{formatDuration(s.remaining_min)} left</span>
          {eta && <span className="font-semibold text-info">Done at {eta}</span>}
        </div>
      )}
    </div>
  )
}

function InfoChips({ snap, s }: { snap: Snapshot; s: Status }) {
  const pickSpeed = useSpeedPicker(snap, s)
  return (
    <div className="grid grid-cols-2 gap-3">
      <button className="card min-w-0 px-3 py-2.5 text-left transition hover:border-accent/40" onClick={pickSpeed}>
        <Caption>Speed ✎</Caption>
        <div className="truncate font-bold">
          <SpeedValue snap={snap} s={s} />
        </div>
      </button>
      <div className="card min-w-0 px-3 py-2.5">
        <Caption>Filament</Caption>
        <div className="text-sm font-semibold">
          <FilamentValue s={s} />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- tablet / desktop

function Ring({ pct, color, children }: { pct: number; color: string; children?: ReactNode }) {
  // 270° arc open at the bottom, like the panel's lv_arc. Scales with its box.
  const r = 42
  const len = 2 * Math.PI * r * 0.75
  const off = len * (1 - Math.max(0, Math.min(100, pct)) / 100)
  return (
    <div className="@container relative aspect-square h-full max-h-[min(100%,560px)] max-w-full">
      <svg viewBox="0 0 100 100" className="h-full w-full rotate-[135deg]">
        <circle cx="50" cy="50" r={r} fill="none" stroke={C.card2} strokeWidth="6" strokeLinecap="round" strokeDasharray={`${len} 999`} />
        <circle
          cx="50"
          cy="50"
          r={r}
          fill="none"
          stroke={color}
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={`${len} 999`}
          strokeDashoffset={off}
          style={{ filter: `drop-shadow(0 0 3px ${color})`, transition: 'stroke-dashoffset .6s ease' }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">{children}</div>
    </div>
  )
}

function StatusCard({ snap, s, onFocus }: { snap: Snapshot; s: Status; onFocus: () => void }) {
  const printing = isPrinting(s)
  const job = hasJob(s)
  const pct = job ? s.progress : 0
  const eta = printing ? formatEta(s.remaining_min) : null
  const pickSpeed = useSpeedPicker(snap, s)
  const sub = job && s.total_layers > 0 ? `Layer ${s.layer} / ${s.total_layers}` : s.connected ? stateText(s) : 'Printer offline'
  return (
    <div className="card flex min-h-0 flex-col p-4 landscape:row-span-3 lg:p-5 portrait:grid portrait:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)] portrait:gap-x-6 portrait:gap-y-4">
      <div className="portrait:col-start-2 portrait:row-start-1 portrait:self-end">
        <Caption>Job</Caption>
        <div className="line-clamp-2 text-lg leading-snug font-bold [overflow-wrap:anywhere] lg:text-xl">{jobTitle(s)}</div>
      </div>

      <button
        className="my-3 flex min-h-[180px] flex-1 items-center justify-center rounded-2xl transition hover:bg-card2/40 portrait:col-start-1 portrait:row-span-2 portrait:row-start-1 portrait:my-0 portrait:h-[min(40vw,340px)]"
        onClick={onFocus}
        title="Focus view"
      >
        <Ring pct={pct} color={progressColor(s)}>
          <div className="text-[22cqw] leading-none font-extrabold tabular-nums" style={{ color: job ? C.text : C.muted }}>
            {Math.floor(pct)}%
          </div>
          <div className="mt-[3cqw] text-[6.5cqw] font-semibold text-muted">{sub}</div>
          {printing && snap.layer_stats.avg_sec > 0 && (
            <div className="mt-[1cqw] text-[5cqw] font-semibold text-muted">{fmtLayer(snap.layer_stats.avg_sec)} / layer</div>
          )}
        </Ring>
      </button>

      <div className="grid grid-cols-2 gap-x-4 gap-y-3 portrait:col-start-2 portrait:row-start-2 portrait:self-start">
        <div>
          <Caption>Remaining</Caption>
          <div className="text-xl font-bold tabular-nums lg:text-2xl">{printing ? formatDuration(s.remaining_min) : '--'}</div>
        </div>
        <div>
          <Caption>Done at</Caption>
          <div className="text-xl font-bold text-info tabular-nums lg:text-2xl">{eta ?? '--'}</div>
        </div>
        <button className="-mx-2 -my-1 rounded-xl px-2 py-1 text-left transition hover:bg-card2" onClick={pickSpeed}>
          <Caption>Speed ✎</Caption>
          <div className="truncate text-lg font-bold">
            <SpeedValue snap={snap} s={s} />
          </div>
        </button>
        <div className="min-w-0">
          <Caption>Filament</Caption>
          <div className="pt-1 text-sm font-semibold">
            <FilamentValue s={s} />
          </div>
        </div>
      </div>
    </div>
  )
}

function CoverCard({ snap, s, onZoom }: { snap: Snapshot; s: Status; onZoom: () => void }) {
  const [mode] = useMediaMode()
  return (
    <div className="card relative flex min-h-[180px] items-center justify-center overflow-hidden p-3">
      <MediaView snap={snap} className="h-full max-h-[560px] w-full" onClick={snap.cover_job ? onZoom : undefined} />
      {snap.cover_job && !isPrinting(s) && mode === 'preview' && (
        <span className="absolute top-3 left-3 rounded-md bg-black/60 px-2 py-0.5 text-xs font-semibold text-muted">
          Last print · {stateText(s)}
        </span>
      )}
    </div>
  )
}

function ZoomCover({
  snap,
  s,
  onClose,
  onReprint,
}: {
  snap: Snapshot
  s: Status
  onClose: () => void
  onReprint: (src: ReprintSource) => void
}) {
  const printing = isPrinting(s)
  const src = useReprintSource(snap, snap.cover_job, !printing && s.connected)
  return (
    <Modal
      title={snap.cover_job}
      onClose={onClose}
      wide
      footer={
        !printing && s.connected && src ? (
          <button className="btn-accent" onClick={() => onReprint(src)}>
            Reprint {src.kind === 'sd' ? '(SD card)' : '(Bambu Cloud)'}
          </button>
        ) : undefined
      }
    >
      <div className="flex flex-col gap-4 sm:flex-row">
        <CoverImage snap={snap} className="aspect-square w-full sm:w-2/3" />
        <div className="whitespace-pre-line text-dim">
          {printing ? `${stateText(s)} ${Math.floor(s.progress)}%\nLayer ${s.layer} / ${s.total_layers}` : `Last print\n${stateText(s)}`}
        </div>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------- temperatures

function TempTile({
  label,
  cur,
  target,
  available = true,
  compact,
  onSet,
  onChart,
}: {
  label: string
  cur: number
  target: number
  available?: boolean
  compact?: boolean
  onSet: () => void
  onChart: () => void
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onSet}
      className={`card flex min-w-0 cursor-pointer flex-col items-start transition hover:border-accent/40 ${compact ? 'px-3 py-2.5' : 'p-4'}`}
    >
      <Caption>{label}</Caption>
      <button
        className={`font-extrabold tabular-nums ${compact ? 'text-2xl' : 'text-3xl lg:text-4xl'}`}
        style={{ color: available ? tempColor(cur, target) : C.muted }}
        onClick={(e) => {
          e.stopPropagation()
          onChart()
        }}
      >
        {available ? `${Math.round(cur)}°` : '--'}
      </button>
      <div className="truncate text-xs text-muted sm:text-sm">
        {!available ? 'not available' : target > 0 ? `Target ${Math.round(target)}°` : 'Off'}
      </div>
    </div>
  )
}

function TempTiles({ snap, s, compact }: { snap: Snapshot; s: Status; compact?: boolean }) {
  const dlg = useDialogs()
  const [chart, setChart] = useState(false)
  const showChamber = snap.settings.chamber_control
  const set = async (which: 'nozzle' | 'bed' | 'chamber', title: string, max: number, cur: number, presets: number[]) => {
    if (which === 'chamber' && !s.chamber_heater) {
      dlg.toast(false, 'This printer has no chamber heater')
      return
    }
    const v = await dlg.numpad({ title, min: 0, max, value: Math.round(cur), unit: '°C', presets })
    if (v !== null) await cmd('set_temp', { which, target: v })
  }
  return (
    <div className={`grid gap-3 ${showChamber ? 'grid-cols-3' : 'grid-cols-2'}`}>
      <TempTile
        label="Nozzle"
        compact={compact}
        cur={s.nozzle}
        target={s.nozzle_target}
        onSet={() => set('nozzle', 'Nozzle target °C', 320, s.nozzle_target, [0, 200, 220, 250])}
        onChart={() => setChart(true)}
      />
      <TempTile
        label="Bed"
        compact={compact}
        cur={s.bed}
        target={s.bed_target}
        onSet={() => set('bed', 'Bed target °C', 140, s.bed_target, [0, 55, 65, 90])}
        onChart={() => setChart(true)}
      />
      {showChamber && (
        <TempTile
          label="Chamber"
          compact={compact}
          cur={s.chamber}
          target={s.chamber_target}
          available={s.has_chamber}
          onSet={() => set('chamber', 'Chamber target °C', 65, s.chamber_target, [0, 35, 45, 55])}
          onChart={() => setChart(true)}
        />
      )}
      {chart && <TempChart onClose={() => setChart(false)} />}
    </div>
  )
}

type Point = [number, number, number, number, number, number | null]

export function TempChart({ onClose }: { onClose: () => void }) {
  const [pts, setPts] = useState<Point[] | null>(null)
  useEffect(() => {
    let alive = true
    const load = () =>
      api<{ points: Point[] }>('/api/temp-history')
        .then((d) => alive && setPts(d.points))
        .catch(() => {})
    load()
    const t = window.setInterval(load, 10000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [])
  const W = 600
  const H = 260
  const now = Date.now() / 1000
  const x = (t: number) => W - ((now - t) / 3600) * W
  const vals = (pts ?? []).flatMap((p) => [p[1], p[2], p[3], p[4], p[5] ?? 0])
  const ymax = Math.max(50, Math.ceil(Math.max(0, ...vals) / 20) * 20 + 10)
  const y = (v: number) => H - (v / ymax) * H
  const line = (i: number) =>
    (pts ?? [])
      .filter((p) => p[i] !== null)
      .map((p, k) => `${k ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[i] as number).toFixed(1)}`)
      .join('')
  const hasChamber = (pts ?? []).some((p) => p[5] !== null)
  const last = pts?.[pts.length - 1]
  const series: [number, string, boolean][] = [
    [2, '#7A4A35', true],
    [4, '#2B4E73', true],
    [1, C.heat, false],
    [3, C.info, false],
    ...(hasChamber ? ([[5, C.accent, false]] as [number, string, boolean][]) : []),
  ]
  return (
    <Modal title="Temperatures - last hour" onClose={onClose} wide>
      {!pts ? (
        <p className="text-muted">Loading...</p>
      ) : (
        <>
          {last && (
            <div className="mb-3 flex flex-wrap gap-x-5 gap-y-1 text-sm font-semibold">
              <span style={{ color: C.heat }}>
                Nozzle {Math.round(last[1])}° / {Math.round(last[2])}°
              </span>
              <span style={{ color: C.info }}>
                Bed {Math.round(last[3])}° / {Math.round(last[4])}°
              </span>
              {hasChamber && last[5] !== null && <span style={{ color: C.accent }}>Chamber {Math.round(last[5])}°</span>}
            </div>
          )}
          <svg viewBox={`0 0 ${W} ${H + 20}`} className="w-full">
            {[0, 0.25, 0.5, 0.75, 1].map((f) => (
              <g key={f}>
                <line x1="0" x2={W} y1={y(ymax * f)} y2={y(ymax * f)} stroke={C.card2} strokeWidth="1" />
                <text x="4" y={y(ymax * f) - 3} fill={C.muted} fontSize="11">
                  {Math.round(ymax * f)}°
                </text>
              </g>
            ))}
            {series.map(([i, color, dashed]) => (
              <path
                key={i}
                d={line(i)}
                fill="none"
                stroke={color}
                strokeWidth={dashed ? 2 : 2.5}
                strokeDasharray={dashed ? '6 4' : undefined}
                strokeLinejoin="round"
              />
            ))}
            <text x="2" y={H + 16} fill={C.muted} fontSize="11">
              -60 min
            </text>
            <text x={W / 2} y={H + 16} fill={C.muted} fontSize="11" textAnchor="middle">
              -30
            </text>
            <text x={W - 2} y={H + 16} fill={C.muted} fontSize="11" textAnchor="end">
              now
            </text>
          </svg>
          <p className="mt-2 text-xs text-muted">Dark dashed lines: target temperatures. One point every 10 s.</p>
        </>
      )}
    </Modal>
  )
}

// ---------------------------------------------------------------- controls

function Controls({ snap, s, compact }: { snap: Snapshot; s: Status; compact?: boolean }) {
  const dlg = useDialogs()
  const ready = printerReady(snap)
  const printing = isPrinting(s)
  const plateClear = plateClearNeeded(snap)
  const [skip, setSkip] = useState(false)
  const skipPress = useSkipButton(snap, () => setSkip(true))
  const oneObject = snap.objects_count === 1

  const stop = async () => {
    const ok = await dlg.confirm({
      title: 'Stop print?',
      text: 'The current print will be cancelled. This cannot be undone.',
      ok: 'Stop print',
      tone: 'danger',
    })
    if (ok) await cmd('stop')
  }
  const clearPlate = async () => {
    const ok = await dlg.confirm({
      title: 'Plate cleared?',
      text: 'Confirm the build plate is empty and back in place. The next plate of the run starts.',
      ok: 'Plate is clear',
    })
    if (ok) await cmd('clear_plate')
  }

  const h = compact ? 'h-16 flex-col gap-1 text-xs' : 'h-16 text-base lg:h-20'
  return (
    <div className={`grid gap-3 ${printing && !plateClear ? 'grid-cols-4' : 'grid-cols-3'}`}>
      {plateClear ? (
        <button className={`btn-accent col-span-2 ${h}`} onClick={clearPlate}>
          PLATE CLEAR
        </button>
      ) : (
        <>
          {s.state === 'PAUSE' ? (
            <button className={`btn-accent ${h}`} disabled={!ready} onClick={() => cmd('resume')}>
              <Play size={20} /> Resume
            </button>
          ) : (
            <button className={`btn ${h}`} disabled={!ready || !printing} onClick={() => cmd('pause')}>
              <Pause size={20} /> Pause
            </button>
          )}
          {printing && (
            <button {...skipPress} className={`btn ${h} ${oneObject ? 'text-muted' : ''}`} disabled={!ready}>
              <ListX size={20} /> Skip
            </button>
          )}
          <button className={`btn-danger ${h}`} disabled={!ready || !printing} onClick={stop}>
            <Square size={20} /> Stop
          </button>
        </>
      )}
      <button className={`${s.light ? 'btn-warn' : 'btn'} ${h}`} disabled={!ready} onClick={() => cmd('light', { on: !s.light })}>
        {s.light ? <Lightbulb size={20} /> : <LightbulbOff size={20} />}
        {s.light ? 'Light on' : 'Light off'}
      </button>
      {skip && <SkipDialog snap={snap} onClose={() => setSkip(false)} />}
    </div>
  )
}

// ---------------------------------------------------------------- focus view

function FocusView({ snap, onClose }: { snap: Snapshot; onClose: () => void }) {
  const [now, setNow] = useState(() => new Date())
  const [skip, setSkip] = useState(false)
  const [lapse, setLapse] = useState(false)
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(t)
  }, [])
  const s = snap.status!
  const printing = isPrinting(s)
  const pct = printing || s.state === 'FINISH' ? s.progress : 0
  const eta = printing ? formatEta(s.remaining_min) : null
  const ls = snap.layer_stats
  const color = progressColor(s)
  return (
    <div
      className="safe-top safe-bottom safe-x fixed inset-0 z-50 flex cursor-pointer flex-col overflow-y-auto bg-bg p-5 md:p-10"
      onClick={onClose}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="text-muted">{now.toLocaleDateString('en-GB', { weekday: 'long', day: '2-digit', month: 'long' })}</div>
        <div className="text-5xl font-extrabold tabular-nums sm:text-6xl md:text-8xl">{clock(now)}</div>
      </div>
      <div className="flex flex-1 flex-col items-center justify-center gap-6 py-4 landscape:flex-row md:gap-10">
        {snap.cover_job && <CoverImage snap={snap} className="aspect-square w-[min(60vw,40vh)] md:w-[min(32vw,50vh)]" />}
        <div className="flex w-full max-w-xl flex-col gap-3">
          <div className="line-clamp-2 text-xl font-bold [overflow-wrap:anywhere] md:text-2xl">{s.job || stateText(s)}</div>
          <div className="text-6xl font-extrabold tabular-nums neon-text md:text-7xl">{Math.floor(pct)}%</div>
          <div className="h-3 overflow-hidden rounded-full bg-card2">
            <div className="h-full rounded-full" style={{ width: `${pct}%`, background: color, boxShadow: `0 0 12px ${color}` }} />
          </div>
          <div className="text-lg font-semibold" style={{ color: stateColor(s) }}>
            {stateText(s)}
            {s.total_layers > 0 && `   Layer ${s.layer} / ${s.total_layers}`}
          </div>
          {printing && ls.avg_sec > 0 && (
            <div className="text-muted">
              {ls.avg_sec} s / layer, last {ls.last_sec} s
            </div>
          )}
          {printing && (
            <div className="text-xl font-semibold">
              {formatDuration(s.remaining_min)} left{eta && <span className="ml-3 text-info">→ done {eta}</span>}
            </div>
          )}
        </div>
      </div>
      <div className="flex items-center justify-between gap-3">
        <div className="text-xs text-muted">Tap anywhere to close</div>
        {snap.timelapse_id && (
          <button
            className="btn h-14 px-5"
            onClick={(e) => {
              e.stopPropagation()
              setLapse(true)
            }}
          >
            <Film size={20} /> Timelapse
          </button>
        )}
        {printing && snap.objects_count > 1 && (
          <button
            className="btn h-14 px-5"
            onClick={(e) => {
              e.stopPropagation()
              setSkip(true)
            }}
          >
            <ListX size={20} /> Skip objects
          </button>
        )}
      </div>
      {lapse && (
        <div onClick={(e) => e.stopPropagation()}>
          <TimelapsePlayer id={snap.timelapse_id} onClose={() => setLapse(false)} />
        </div>
      )}
      {skip && (
        <div onClick={(e) => e.stopPropagation()}>
          <SkipDialog snap={snap} onClose={() => setSkip(false)} />
        </div>
      )}
    </div>
  )
}
