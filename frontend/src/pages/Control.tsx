// CONTROL: Move + Temp & fans (panel src/ui/pages/ControlPage.cpp, MovePage.cpp, PrinterPage.cpp).
// Large landscape screens show both side by side; smaller ones switch with a segmented control.
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, ChevronsDown, ChevronsUp, Flame, Home as HomeIcon, Minus, Plus, Power, TriangleAlert } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { cmd } from '../api'
import { activeTray, C, isPrinting, online, printerReady, tempColor } from '../format'
import type { Snapshot, Status } from '../types'
import { useDialogs } from '../ui'

type Tab = 'move' | 'temps'

export default function Control({ snap }: { snap: Snapshot | null }) {
  const [tab, setTab] = useState<Tab>('move')
  if (!snap || !online(snap)) {
    return (
      <div className="flex h-full min-h-[50vh] flex-col items-center justify-center gap-3 p-6 text-center">
        <TriangleAlert size={48} className="text-warn" />
        <div className="text-lg font-bold">Not connected</div>
        <div className="text-muted">{snap?.link_text || 'Connecting to the server...'}</div>
      </div>
    )
  }
  const s = snap.status!
  return (
    <div className="mx-auto flex max-w-[1500px] flex-col gap-4 p-3 md:p-4">
      <div className="grid grid-cols-2 gap-1 rounded-2xl bg-panel p-1 lg:landscape:hidden">
        {(['move', 'temps'] as Tab[]).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`h-11 rounded-xl text-sm font-bold transition ${tab === t ? 'bg-card2 text-accent shadow' : 'text-muted hover:text-text'}`}
          >
            {t === 'move' ? 'Move' : 'Temp & fans'}
          </button>
        ))}
      </div>
      <div className="grid gap-4 lg:landscape:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <div className={`${tab === 'move' ? '' : 'hidden'} lg:landscape:block`}>
          <MovePanel snap={snap} s={s} />
        </div>
        <div className={`${tab === 'temps' ? '' : 'hidden'} lg:landscape:block`}>
          <TempsPanel snap={snap} s={s} />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- shared

function Card({ title, right, children, className = '' }: { title: string; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card p-4 ${className}`}>
      <div className="mb-3 flex items-center gap-3">
        <h2 className="flex-1 text-xs font-bold tracking-[0.18em] text-muted uppercase">{title}</h2>
        {right}
      </div>
      {children}
    </section>
  )
}

export function Steps({ values, value, onChange }: { values: number[]; value: number; onChange: (v: number) => void }) {
  return (
    <div className="flex rounded-xl bg-panel p-0.5">
      {values.map((v) => (
        <button
          key={v}
          onClick={() => onChange(v)}
          className={`h-8 min-w-11 rounded-lg px-2 text-xs font-bold tabular-nums transition ${value === v ? 'bg-accent text-black' : 'text-muted hover:text-text'}`}
        >
          {v}
        </button>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------- move

/** Move / extrude actions with the panel's rules (no movement while printing, 100 mm asks first). */
export function useMove(snap: Snapshot, s: Status) {
  const dlg = useDialogs()
  const ready = printerReady(snap)
  const printing = isPrinting(s)
  const canMove = ready && !printing
  const hot = s.nozzle >= 170
  const canExtrude = canMove && hot

  const endstopNote = ' Soft endstops are not enforced for remote jogs - make sure the path is clear.'
  const move = async (name: string, args: Record<string, unknown>, step: number, text: string) => {
    if (step >= 100) {
      const ok = await dlg.confirm({ title: 'Large move', text: text + endstopNote, ok: 'Move', tone: 'warn' })
      if (!ok) return
    }
    await cmd(name, args)
  }
  const jogXY = (dx: number, dy: number, step: number) => move('jog_xy', { x: dx * step, y: dy * step }, step, `Move ${step} mm?`)
  const jogZ = (dir: number, step: number) =>
    move('jog_z', { distance: dir * step }, step, `Move the bed ${step} mm ${dir < 0 ? 'up' : 'down'}?`)
  const extrude = async (dir: number, step: number) => {
    if (step >= 100) {
      const ok = await dlg.confirm({
        title: dir > 0 ? `Extrude ${step} mm?` : `Retract ${step} mm?`,
        text: dir > 0 ? `Pushes ${step} mm of filament through the hot nozzle.` : `Pulls ${step} mm of filament back.`,
        ok: dir > 0 ? 'Extrude' : 'Retract',
        tone: 'warn',
      })
      if (!ok) return
    }
    await cmd('extrude', { distance: dir * step })
  }
  const tray = activeTray(s)
  const preheat = tray && tray.temp_max >= 170 && tray.temp_max <= 320 ? tray.temp_max : 220
  return { ready, printing, canMove, hot, canExtrude, jogXY, jogZ, extrude, tray, preheat }
}

function MovePanel({ snap, s }: { snap: Snapshot; s: Status }) {
  const [xyStep, setXyStep] = useState(10)
  const [zStep, setZStep] = useState(1)
  const [eStep, setEStep] = useState(10)
  const m = useMove(snap, s)
  const { ready, printing, canMove, hot, canExtrude, tray, preheat } = m
  const jogXY = (dx: number, dy: number) => m.jogXY(dx, dy, xyStep)
  const jogZ = (dir: number) => m.jogZ(dir, zStep)
  const extrude = (dir: number) => m.extrude(dir, eStep)

  let nozzleLine: ReactNode = 'Nozzle --'
  if (ready) {
    if (hot) nozzleLine = <span className="text-accent">Nozzle {Math.round(s.nozzle)}°C ✓</span>
    else if (s.nozzle_target >= 170) nozzleLine = `Nozzle ${Math.round(s.nozzle)} / ${Math.round(s.nozzle_target)}°C, heating`
    else nozzleLine = `Nozzle ${Math.round(s.nozzle)}°C - too cold`
  }

  const padBtn = 'btn aspect-square h-auto w-full rounded-2xl p-0 text-text'
  return (
    <div className="flex flex-col gap-4">
      {!ready ? (
        <Notice warn>⚠ Printer not reachable.</Notice>
      ) : printing ? (
        <Notice warn>⚠ Movement is disabled while a print is running.</Notice>
      ) : (
        <Notice>Home first. Remote jogs do not check soft endstops; 100 mm moves ask first.</Notice>
      )}

      <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Card title="X / Y" right={<Steps values={[1, 10, 100]} value={xyStep} onChange={setXyStep} />}>
          <div className="mx-auto grid w-full max-w-[min(300px,58vw)] grid-cols-3 gap-2">
            <div />
            <button className={padBtn} disabled={!canMove} onClick={() => jogXY(0, 1)} aria-label="Y+">
              <ArrowUp size={28} />
              <span className="sr-only">Y+</span>
            </button>
            <div />
            <button className={padBtn} disabled={!canMove} onClick={() => jogXY(-1, 0)} aria-label="X-">
              <ArrowLeft size={28} />
            </button>
            <button
              className={`${padBtn} flex-col gap-0.5 text-xs`}
              disabled={!canMove}
              onClick={() => cmd('home', { axes: 'xy' })}
              title="Home X / Y"
            >
              <HomeIcon size={24} />
              XY
            </button>
            <button className={padBtn} disabled={!canMove} onClick={() => jogXY(1, 0)} aria-label="X+">
              <ArrowRight size={28} />
            </button>
            <div />
            <button className={padBtn} disabled={!canMove} onClick={() => jogXY(0, -1)} aria-label="Y-">
              <ArrowDown size={28} />
            </button>
            <div />
          </div>
          <div className="mt-2 flex justify-between text-[11px] font-semibold text-muted">
            <span>X− / X+</span>
            <span>Y+ up, Y− down</span>
          </div>
        </Card>

        <div className="flex flex-col gap-4">
          <Card title="Z (bed)" right={<Steps values={[0.1, 1, 10, 100]} value={zStep} onChange={setZStep} />}>
            <div className="grid grid-cols-3 gap-2">
              <button className="btn h-14 flex-col gap-0 text-xs" disabled={!canMove} onClick={() => jogZ(-1)}>
                <ChevronsUp size={22} /> Bed up
              </button>
              <button className="btn h-14 flex-col gap-0 text-xs" disabled={!canMove} onClick={() => jogZ(1)}>
                <ChevronsDown size={22} /> Bed down
              </button>
              <button className="btn h-14 flex-col gap-0 text-xs" disabled={!canMove} onClick={() => cmd('home', { axes: 'z' })}>
                <HomeIcon size={20} /> Home Z
              </button>
            </div>
          </Card>

          <Card title="Extruder" right={<Steps values={[1, 10, 100]} value={eStep} onChange={setEStep} />}>
            <div className="grid grid-cols-2 gap-2">
              <button className="btn h-14" disabled={!canExtrude} onClick={() => extrude(-1)}>
                <Minus size={18} /> Retract
              </button>
              <button className="btn h-14" disabled={!canExtrude} onClick={() => extrude(1)}>
                <Plus size={18} /> Extrude
              </button>
            </div>
            <div className="mt-3 flex items-center gap-3">
              <div className="min-w-0 flex-1 text-sm font-semibold text-muted">{nozzleLine}</div>
              {s.nozzle_target > 0 ? (
                <button className="btn" disabled={!canMove} onClick={() => cmd('set_temp', { which: 'nozzle', target: 0 })}>
                  <Power size={16} /> Nozzle off
                </button>
              ) : (
                <button
                  className="btn"
                  disabled={!canMove}
                  onClick={() => cmd('set_temp', { which: 'nozzle', target: preheat })}
                  title={tray?.type ? `Preheat for ${tray.type}` : 'Preheat'}
                >
                  <Flame size={16} className="text-heat" /> Preheat {preheat}°C
                  {tray?.type && <span className="text-muted">· {tray.type}</span>}
                </button>
              )}
            </div>
          </Card>
        </div>
      </div>

      <button className="btn h-14 text-base" disabled={!canMove} onClick={() => cmd('home', { axes: 'all' })}>
        <HomeIcon size={20} /> Home all axes
      </button>
    </div>
  )
}

function Notice({ children, warn }: { children: ReactNode; warn?: boolean }) {
  return (
    <div className={`rounded-xl px-4 py-2.5 text-sm font-semibold ${warn ? 'bg-warn/12 text-warn' : 'bg-panel text-muted'}`}>{children}</div>
  )
}

// ---------------------------------------------------------------- temperatures & fans

function TempsPanel({ snap, s }: { snap: Snapshot; s: Status }) {
  const dlg = useDialogs()
  const ready = printerReady(snap)
  const rows: { which: 'nozzle' | 'bed' | 'chamber'; label: string; max: number; cur: number; target: number; available: boolean; settable: boolean; presets: number[] }[] = [
    { which: 'nozzle', label: 'Nozzle', max: 320, cur: s.nozzle, target: s.nozzle_target, available: true, settable: true, presets: [0, 200, 220, 250] },
    { which: 'bed', label: 'Bed', max: 140, cur: s.bed, target: s.bed_target, available: true, settable: true, presets: [0, 55, 65, 90] },
  ]
  if (snap.settings.chamber_control)
    rows.push({
      which: 'chamber',
      label: 'Chamber',
      max: 65,
      cur: s.chamber,
      target: s.chamber_target,
      available: s.has_chamber,
      settable: s.chamber_heater,
      presets: [0, 35, 45, 55],
    })

  const setTarget = async (r: (typeof rows)[number]) => {
    const v = await dlg.numpad({ title: `${r.label} target °C`, min: 0, max: r.max, value: Math.round(r.target), unit: '°C', presets: r.presets })
    if (v !== null) await cmd('set_temp', { which: r.which, target: v })
  }

  return (
    <div className="flex flex-col gap-4">
      <Card title="Temperatures">
        <div className="flex flex-col divide-y divide-border/50">
          {rows.map((r) => (
            <div key={r.which} className="flex items-center gap-3 py-3 first:pt-0 last:pb-0">
              <div className="w-20 shrink-0 font-semibold">{r.label}</div>
              <div className="w-16 shrink-0 text-2xl font-extrabold tabular-nums" style={{ color: r.available ? tempColor(r.cur, r.target) : C.muted }}>
                {r.available ? `${Math.round(r.cur)}°` : '--'}
              </div>
              <div className="flex-1" />
              <button className="btn min-w-20 tabular-nums" disabled={!ready || !r.settable} onClick={() => setTarget(r)}>
                {!r.settable ? 'n/a' : r.target > 0 ? <span style={{ color: tempColor(r.cur, r.target) }}>{Math.round(r.target)}°</span> : 'Set'}
              </button>
              <button
                className="btn"
                disabled={!ready || !r.settable || r.target <= 0}
                onClick={() => cmd('set_temp', { which: r.which, target: 0 })}
              >
                Off
              </button>
            </div>
          ))}
        </div>
      </Card>

      <Card title="Fans">
        <div className="flex flex-col gap-4">
          <FanRow label="Part" fan="part" value={s.fan_part} disabled={!ready} />
          <FanRow label="Aux" fan="aux" value={s.fan_aux} disabled={!ready} />
          <FanRow label="Chamber" fan="chamber" value={s.fan_chamber} disabled={!ready} />
        </div>
      </Card>
    </div>
  )
}

export function FanRow({ label, fan, value, disabled }: { label: string; fan: string; value: number; disabled: boolean }) {
  // While dragging, the live value from the printer does not overwrite the slider.
  const [drag, setDrag] = useState<number | null>(null)
  const shown = drag ?? Math.max(0, value)
  const commit = async () => {
    if (drag === null) return
    const v = drag
    await cmd('fan', { fan, speed: v })
    setDrag(null)
  }
  return (
    <div className="flex items-center gap-4">
      <div className="w-20 shrink-0 font-semibold">{label}</div>
      <input
        type="range"
        min={0}
        max={100}
        step={5}
        value={shown}
        disabled={disabled}
        onChange={(e) => setDrag(Number(e.target.value))}
        onPointerUp={commit}
        onKeyUp={commit}
        onTouchEnd={commit}
        className="h-2 min-w-0 flex-1 cursor-pointer accent-[var(--color-accent)] disabled:opacity-40"
      />
      <div className="w-12 shrink-0 text-right font-bold tabular-nums">{value < 0 && drag === null ? '--' : `${shown}%`}</div>
    </div>
  )
}
