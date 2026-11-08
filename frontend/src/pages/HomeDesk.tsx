// HOME on a laptop / desktop: a dense web dashboard (in the spirit of Bambu Studio's Device
// page): camera / preview + the print on the left; temperatures, fans, move and AMS on the right.
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  ChevronsDown,
  ChevronsUp,
  Clock,
  Film,
  Gauge,
  Home as HomeIcon,
  Layers,
  LineChart,
  Lightbulb,
  LightbulbOff,
  ListX,
  Maximize,
  Minus,
  Pause,
  Play,
  Plus,
  Square,
  Timer,
  Zap,
} from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { cmd } from '../api'
import { C, formatDuration, formatEta, hasJob, isPrinting, plateClearNeeded, printerReady, stateColor, stateText, tempColor } from '../format'
import { MediaView } from '../media'
import { TimelapsePlayer } from '../timelapse'
import { SkipDialog, useSkipButton } from '../PrintScreens'
import type { Snapshot, Status } from '../types'
import { useDialogs } from '../ui'
import { AmsMini } from './Ams'
import { FanRow, Steps, useMove } from './Control'
import { CoverImage, FilamentValue, jobTitle, progressColor, SpeedValue, TempChart, useSpeedPicker } from './Home'

export function DeskHome({
  snap,
  s,
  onZoom,
  onFocus,
  goPage,
}: {
  snap: Snapshot
  s: Status
  onZoom: () => void
  onFocus: () => void
  goPage: (p: 'ams' | 'control') => void
}) {
  const printing = isPrinting(s)
  return (
    <div className="mx-auto hidden h-full max-w-[1800px] grid-cols-[minmax(0,1.45fr)_minmax(400px,1fr)] grid-rows-[minmax(0,1fr)_auto] gap-4 p-4 desk:grid">
      <div className="card min-h-0 p-2">
        <MediaView snap={snap} className="h-full w-full" onClick={onZoom} />
      </div>
      {/* Printing: Move is one line (disabled) on top of the right column. Idle: the full Move
          pad sits next to the print card, so the right column fits without scrolling. */}
      <div className="row-span-2 flex min-h-0 flex-col gap-3 overflow-y-auto [&>*]:shrink-0">
        {printing && <MoveCard snap={snap} s={s} goControl={() => goPage('control')} />}
        <PrinterCard snap={snap} s={s} />
        <AmsMini snap={snap} goAms={() => goPage('ams')} fill />
      </div>
      {printing ? (
        <ProgressCard snap={snap} s={s} onZoom={onZoom} onFocus={onFocus} />
      ) : (
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] gap-4">
          <ProgressCard snap={snap} s={s} onZoom={onZoom} onFocus={onFocus} />
          <MoveCard snap={snap} s={s} goControl={() => goPage('control')} />
        </div>
      )}
    </div>
  )
}

/** 42 s, 3m 05s */
export function fmtLayer(sec: number): string {
  if (sec < 0) return '--'
  return sec < 120 ? `${sec} s` : `${Math.floor(sec / 60)}m ${String(sec % 60).padStart(2, '0')}s`
}

function Title({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-2.5 flex items-center gap-2">
      <span className="text-xs font-bold tracking-[0.18em] text-muted uppercase">{children}</span>
      <div className="h-px flex-1 bg-border/60" />
      {right}
    </div>
  )
}

// ---------------------------------------------------------------- the print

function ProgressCard({ snap, s, onZoom, onFocus }: { snap: Snapshot; s: Status; onZoom: () => void; onFocus: () => void }) {
  const dlg = useDialogs()
  const ready = printerReady(snap)
  const printing = isPrinting(s)
  const job = hasJob(s)
  const pct = job ? s.progress : 0
  const eta = printing ? formatEta(s.remaining_min) : null
  const color = progressColor(s)
  const pickSpeed = useSpeedPicker(snap, s)
  const [skip, setSkip] = useState(false)
  const skipPress = useSkipButton(snap, () => setSkip(true))
  const [lapse, setLapse] = useState(false)
  const stop = async () => {
    const ok = await dlg.confirm({ title: 'Stop print?', text: 'The current print will be cancelled. This cannot be undone.', ok: 'Stop print', tone: 'danger' })
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
  // Idle: Pause / Stop would only be disabled buttons.
  const buttons = !printing && !plateClearNeeded(snap) ? null : plateClearNeeded(snap) ? (
    <button className="btn-accent h-10 px-4" onClick={clearPlate}>
      Plate clear
    </button>
  ) : (
    <>
      {s.state === 'PAUSE' ? (
        <button className="btn-accent h-10 px-4" disabled={!ready} onClick={() => cmd('resume')}>
          <Play size={18} /> Resume
        </button>
      ) : (
        <button className="btn h-10 px-4" disabled={!ready || !printing} onClick={() => cmd('pause')}>
          <Pause size={18} /> Pause
        </button>
      )}
      {printing && (
        <button {...skipPress} className={`btn h-10 px-4 ${snap.objects_count === 1 ? 'text-muted' : ''}`} disabled={!ready} title="Skip objects">
          <ListX size={18} /> Skip
        </button>
      )}
      <button className="btn-danger h-10 px-4" disabled={!ready || !printing} onClick={stop}>
        <Square size={18} /> Stop
      </button>
    </>
  )
  return (
    <div className="card flex items-center gap-4 p-4">
      <button onClick={onZoom} className="shrink-0" title="Preview">
        <CoverImage snap={snap} className="h-24 w-24 xl:h-28 xl:w-28" />
      </button>
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex items-center gap-3">
          <div className="min-w-0 flex-1">
            <div className="text-xs font-bold tracking-wider uppercase" style={{ color: stateColor(s) }}>
              {s.connected ? stateText(s) : 'Printer offline'}
            </div>
            <button onClick={onFocus} className="block max-w-full truncate text-left text-lg font-bold hover:text-accent" title={`${jobTitle(s)} - focus view`}>
              {jobTitle(s)}
            </button>
          </div>
          <div className="flex shrink-0 gap-2">
            {snap.timelapse_id && (
              <button className="btn h-10 w-10 p-0" onClick={() => setLapse(true)} title="Timelapse with Obico scores">
                <Film size={18} />
              </button>
            )}
            {buttons}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-card2">
            <div className="h-full rounded-full transition-[width] duration-700" style={{ width: `${pct}%`, background: color, boxShadow: `0 0 10px ${color}` }} />
          </div>
          <span className="w-16 text-right text-2xl font-extrabold tabular-nums">{Math.floor(pct)}%</span>
        </div>
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm">
          {printing && (
            <span className="flex items-center gap-1.5 font-semibold">
              <Clock size={15} className="text-muted" /> {formatDuration(s.remaining_min)}
              {eta && <span className="text-info">→ {eta}</span>}
            </span>
          )}
          {job && s.total_layers > 0 && (
            <span className="flex items-center gap-1.5 font-semibold">
              <Layers size={15} className="text-muted" /> {s.layer} / {s.total_layers}
            </span>
          )}
          {printing && snap.layer_stats.avg_sec > 0 && (
            <span className="flex items-center gap-1.5 font-semibold" title={`Average layer time; the last layer took ${snap.layer_stats.last_sec} s`}>
              <Timer size={15} className="text-muted" /> {fmtLayer(snap.layer_stats.avg_sec)} / layer
              <span className="text-muted">(last {fmtLayer(snap.layer_stats.last_sec)})</span>
            </span>
          )}
          <button className="flex items-center gap-1.5 font-semibold hover:text-accent" onClick={pickSpeed} title="Print speed">
            <Gauge size={15} className="text-muted" /> <SpeedValue snap={snap} s={s} />
          </button>
          <span className="min-w-0 font-semibold">
            <FilamentValue s={s} />
          </span>
        </div>
      </div>
      {skip && <SkipDialog snap={snap} onClose={() => setSkip(false)} />}
      {lapse && <TimelapsePlayer id={snap.timelapse_id} onClose={() => setLapse(false)} />}
    </div>
  )
}

// ---------------------------------------------------------------- temperatures, fans, speed, light

function PrinterCard({ snap, s }: { snap: Snapshot; s: Status }) {
  const dlg = useDialogs()
  const ready = printerReady(snap)
  const [chart, setChart] = useState(false)
  const pickSpeed = useSpeedPicker(snap, s)
  const temps: { which: 'nozzle' | 'bed' | 'chamber'; label: string; cur: number; target: number; max: number; presets: number[]; ok: boolean; settable: boolean }[] = [
    { which: 'nozzle', label: 'Nozzle', cur: s.nozzle, target: s.nozzle_target, max: 320, presets: [0, 200, 220, 250], ok: true, settable: true },
    { which: 'bed', label: 'Bed', cur: s.bed, target: s.bed_target, max: 140, presets: [0, 55, 65, 90], ok: true, settable: true },
  ]
  if (snap.settings.chamber_control)
    temps.push({
      which: 'chamber',
      label: 'Chamber',
      cur: s.chamber,
      target: s.chamber_target,
      max: 65,
      presets: [0, 35, 45, 55],
      ok: s.has_chamber,
      settable: s.chamber_heater,
    })
  const set = async (t: (typeof temps)[number]) => {
    if (!t.settable) return dlg.toast(false, 'This printer has no chamber heater')
    const v = await dlg.numpad({ title: `${t.label} target °C`, min: 0, max: t.max, value: Math.round(t.target), unit: '°C', presets: t.presets })
    if (v !== null) await cmd('set_temp', { which: t.which, target: v })
  }
  return (
    <div className="card p-3.5">
      <Title
        right={
          <button className="text-muted hover:text-accent" onClick={() => setChart(true)} title="Temperature history">
            <LineChart size={16} />
          </button>
        }
      >
        Printer
      </Title>
      <div className={`grid gap-2 ${temps.length === 3 ? 'grid-cols-3' : 'grid-cols-2'}`}>
        {temps.map((t) => (
          <button
            key={t.which}
            disabled={!ready}
            onClick={() => set(t)}
            className="rounded-xl bg-panel px-3 py-2 text-left transition hover:bg-card2 disabled:opacity-60"
          >
            <div className="text-[11px] font-bold tracking-wider text-muted uppercase">{t.label}</div>
            <div className="flex items-baseline gap-1 tabular-nums">
              <span className="text-2xl font-extrabold" style={{ color: t.ok ? tempColor(t.cur, t.target) : C.muted }}>
                {t.ok ? Math.round(t.cur) : '--'}
              </span>
              <span className="text-sm text-muted">/ {t.target > 0 ? `${Math.round(t.target)}°` : 'off'}</span>
            </div>
          </button>
        ))}
      </div>
      <div className="mt-2.5 flex flex-col gap-1.5">
        <FanRow label="Part fan" fan="part" value={s.fan_part} disabled={!ready} />
        <FanRow label="Aux fan" fan="aux" value={s.fan_aux} disabled={!ready} />
        <FanRow label="Chamber" fan="chamber" value={s.fan_chamber} disabled={!ready} />
      </div>
      <div className="mt-2.5 grid grid-cols-2 gap-2">
        <button className="btn h-9 justify-start px-3 text-sm" disabled={!ready} onClick={pickSpeed}>
          <Zap size={16} className="text-muted" /> <SpeedValue snap={snap} s={s} />
        </button>
        <button className={`${s.light ? 'btn-warn' : 'btn'} h-9 text-sm`} disabled={!ready} onClick={() => cmd('light', { on: !s.light })}>
          {s.light ? <Lightbulb size={16} /> : <LightbulbOff size={16} />} {s.light ? 'Light on' : 'Light off'}
        </button>
      </div>
      {chart && <TempChart onClose={() => setChart(false)} />}
    </div>
  )
}

// ---------------------------------------------------------------- move (compact)

function MoveCard({ snap, s, goControl }: { snap: Snapshot; s: Status; goControl: () => void }) {
  const m = useMove(snap, s)
  const [xy, setXy] = useState(10)
  const [z, setZ] = useState(1)
  if (m.printing) {
    return (
      <div className="card flex items-center gap-3 px-4 py-3 text-sm text-muted">
        <span className="text-xs font-bold tracking-[0.18em] uppercase">Move</span>
        <span className="flex-1">Disabled while a print is running.</span>
        <button className="text-xs font-bold text-dim hover:text-accent" onClick={goControl}>
          Control page →
        </button>
      </div>
    )
  }
  const pad = 'btn h-9 w-full p-0'
  return (
    <div className="card p-3.5">
      <Title
        right={
          <button className="flex items-center gap-1 text-xs font-bold text-muted hover:text-accent" onClick={goControl} title="Full move page">
            <Maximize size={13} /> More
          </button>
        }
      >
        Move
      </Title>
      <div className="grid grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)] gap-4">
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold whitespace-nowrap text-muted">XY mm</span>
            <Steps values={[1, 10, 100]} value={xy} onChange={setXy} />
          </div>
          <div className="mx-auto grid w-full max-w-[200px] grid-cols-3 gap-1.5">
            <div />
            <button className={pad} disabled={!m.canMove} onClick={() => m.jogXY(0, 1, xy)} aria-label="Y+">
              <ArrowUp size={18} />
            </button>
            <div />
            <button className={pad} disabled={!m.canMove} onClick={() => m.jogXY(-1, 0, xy)} aria-label="X-">
              <ArrowLeft size={18} />
            </button>
            <button className={`${pad} text-accent`} disabled={!m.canMove} onClick={() => cmd('home', { axes: 'all' })} title="Home all axes">
              <HomeIcon size={18} />
            </button>
            <button className={pad} disabled={!m.canMove} onClick={() => m.jogXY(1, 0, xy)} aria-label="X+">
              <ArrowRight size={18} />
            </button>
            <div />
            <button className={pad} disabled={!m.canMove} onClick={() => m.jogXY(0, -1, xy)} aria-label="Y-">
              <ArrowDown size={18} />
            </button>
            <div />
          </div>
        </div>
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-muted">Bed mm</span>
            <Steps values={[1, 10]} value={z} onChange={setZ} />
          </div>
          <div className="grid grid-cols-2 gap-1.5">
            <button className="btn h-9 p-0 text-xs" disabled={!m.canMove} onClick={() => m.jogZ(-1, z)} title="Bed up">
              <ChevronsUp size={18} />
            </button>
            <button className="btn h-9 p-0 text-xs" disabled={!m.canMove} onClick={() => m.jogZ(1, z)} title="Bed down">
              <ChevronsDown size={18} />
            </button>
          </div>
          <span className="mt-1 text-xs font-semibold text-muted">
            Extruder 10 mm {!m.hot && <span className="text-warn">· nozzle cold</span>}
          </span>
          <div className="grid grid-cols-2 gap-1.5">
            <button className="btn h-9 p-0" disabled={!m.canExtrude} onClick={() => m.extrude(-1, 10)} title="Retract 10 mm">
              <Minus size={18} />
            </button>
            <button className="btn h-9 p-0" disabled={!m.canExtrude} onClick={() => m.extrude(1, 10)} title="Extrude 10 mm">
              <Plus size={18} />
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
