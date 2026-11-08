// AMS: units as tabs, slot cards, slot dialog (load / unload / edit / RFID / spool / reset),
// humidity + temperature history (panel src/ui/pages/AmsPage.cpp).
import { Boxes, Droplets, Eye, EyeOff, Loader2, Pencil, RefreshCw, RotateCcw, Save, ScanLine, Search, Thermometer, TriangleAlert, Unlink, Upload, Download, Link2, Package } from 'lucide-react'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { UsageList } from '../usage'
import { api, cmd, type ApiError } from '../api'
import { C, isPrinting, online, printerReady } from '../format'
import type { AmsUnit, Assignment, Snapshot, Spool, Status, Tray } from '../types'
import { Modal, Swatch, useDialogs } from '../ui'

const EXT_AMS = 255

// Generic Bambu filament presets (panel kMaterials).
const MATERIALS: { type: string; idx: string; min: number; max: number }[] = [
  { type: 'PLA', idx: 'GFL99', min: 190, max: 230 },
  { type: 'PETG', idx: 'GFG99', min: 220, max: 260 },
  { type: 'ABS', idx: 'GFB99', min: 240, max: 270 },
  { type: 'ASA', idx: 'GFB98', min: 240, max: 270 },
  { type: 'TPU', idx: 'GFU99', min: 200, max: 240 },
  { type: 'PA', idx: 'GFN99', min: 260, max: 290 },
  { type: 'PC', idx: 'GFC99', min: 250, max: 280 },
  { type: 'PVA', idx: 'GFS99', min: 190, max: 210 },
  { type: 'HIPS', idx: 'GFS98', min: 220, max: 250 },
  { type: 'PLA-CF', idx: 'GFL98', min: 210, max: 240 },
  { type: 'PETG-CF', idx: 'GFG98', min: 240, max: 270 },
  { type: 'PA-CF', idx: 'GFN98', min: 270, max: 300 },
]

/** One slot: ams_id / tray_id (configure, assignments) and the global id for /ams/load. */
export interface SlotRef {
  amsId: number
  trayId: number
  loadId: number
  label: string
}

export type Tab = { kind: 'unit'; unit: AmsUnit } | { kind: 'ext' }

export function unitName(u: AmsUnit): string {
  return u.is_ht || u.id >= 128 ? `AMS-HT ${u.id - 127}` : `AMS ${String.fromCharCode(65 + u.id)}`
}

export function slotsOf(tab: Tab, s: Status): { ref: SlotRef; tray: Tray | null }[] {
  if (tab.kind === 'ext') return [{ ref: { amsId: EXT_AMS, trayId: 0, loadId: 254, label: 'Ext' }, tray: s.ext[0] ?? null }]
  const u = tab.unit
  if (u.id >= 128)
    return [{ ref: { amsId: u.id, trayId: 0, loadId: u.id, label: `HT${u.id - 127}` }, tray: u.trays[0] ?? null }]
  return [0, 1, 2, 3].map((i) => ({
    ref: { amsId: u.id, trayId: i, loadId: u.id * 4 + i, label: `${String.fromCharCode(65 + u.id)}${i + 1}` },
    tray: u.trays[i] ?? null,
  }))
}

export const inUse = (s: Status, r: SlotRef) => r.loadId >= 0 && r.loadId !== 255 && s.tray_now === r.loadId

export function stateText(s: Status, r: SlotRef, t: Tray | null): string {
  if (inUse(s, r)) return 'IN USE'
  if (!t || !t.has_filament || t.state === 9) return 'EMPTY'
  if (t.state === 11) return 'LOADED'
  return 'READY'
}

export function assignmentFor(as: Assignment[], r: SlotRef): Assignment | null {
  return as.find((a) => a.ams_id === r.amsId && a.tray_id === r.trayId) ?? null
}

function displayName(t: Tray | null, a: Assignment | null): string {
  if (a && (a.spool.brand || a.spool.subtype)) return `${a.spool.brand} ${a.spool.subtype || a.spool.material}`.trim()
  return t?.sub_brands || (t?.has_filament ? 'Generic' : 'No filament')
}

/** Grams left on a spool; `used` = estimated use of the running print (booked when the print ends). */
const spoolLeft = (sp: Spool, used = 0) => Math.max(0, sp.label_weight - sp.weight_used - used)

/** Filament the running print has used from a tray so far (estimate, 0 when not printing). */
export function usedNow(snap: Snapshot, trayId: number): number {
  return snap.filament_use?.by_tray[String(trayId)] ?? 0
}

/** Filament the running print needs from a tray in total (from the file; 0 = unknown). */
export function needOf(snap: Snapshot, trayId: number): number {
  return snap.filament_use?.need_by_tray?.[String(trayId)] ?? 0
}

function UsedNote({ used, long }: { used: number; long?: boolean }) {
  if (used < 0.5) return null
  return <span className="text-info">{long ? ` (−${used.toFixed(1)} g this print)` : ` · −${used.toFixed(0)} g`}</span>
}

/** Remaining %: the spool's weight when known, otherwise the AMS estimate (-1 unknown). */
export function remainPct(t: Tray | null, a: Assignment | null, used = 0): number {
  if (a && a.spool.label_weight > 0) return Math.round((100 * spoolLeft(a.spool, used)) / a.spool.label_weight)
  return t && t.has_filament ? t.remain : -1
}

export const pctColor = (p: number) => (p < 15 ? C.error : p < 30 ? C.warn : C.accent)

// ---------------------------------------------------------------- page

export default function Ams({ snap }: { snap: Snapshot | null }) {
  const dlg = useDialogs()
  const [tabIdx, setTabIdx] = useState(0)
  const [open, setOpen] = useState<SlotRef | null>(null)
  const [chart, setChart] = useState<{ unit: AmsUnit; what: 'hum' | 'temp' } | null>(null)

  if (!snap || !online(snap) || !snap.status) {
    return (
      <div className="flex h-full min-h-[50vh] flex-col items-center justify-center gap-3 p-6 text-center">
        <TriangleAlert size={48} className="text-warn" />
        <div className="text-lg font-bold">Not connected</div>
        <div className="text-muted">{snap?.link_text || 'Connecting to the server...'}</div>
      </div>
    )
  }
  const s = snap.status
  const tabs: Tab[] = [...s.ams.map((unit) => ({ kind: 'unit' as const, unit })), { kind: 'ext' as const }]
  const tab = tabs[Math.min(tabIdx, tabs.length - 1)]
  const slots = slotsOf(tab, s)
  const printing = isPrinting(s)
  const ready = printerReady(snap)

  const unload = async () => {
    const ok = await dlg.confirm({ title: 'Unload filament', text: 'Retract the currently loaded filament back into its slot?', ok: 'Unload' })
    if (ok) await cmd('ams_unload')
  }

  return (
    <div className="mx-auto flex max-w-[1500px] flex-col gap-3 p-3 md:p-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto rounded-xl bg-card p-1">
          {tabs.map((t, i) => (
            <button
              key={i}
              onClick={() => setTabIdx(i)}
              className={`shrink-0 rounded-lg px-3 py-2 text-sm font-bold transition sm:px-4 ${
                tab === t ? 'bg-card2 text-accent shadow-[0_0_14px_-6px_var(--color-accent)]' : 'text-muted hover:text-text'
              }`}
            >
              {t.kind === 'ext' ? 'External' : unitName(t.unit)}
            </button>
          ))}
        </div>
        {tab.kind === 'unit' && (
          <div className="flex gap-2">
            <button className="btn px-3" onClick={() => setChart({ unit: tab.unit, what: 'hum' })} title="Humidity history">
              <Droplets size={18} className={tab.unit.humidity > 40 ? 'text-info' : ''} /> {tab.unit.humidity}%
            </button>
            {tab.unit.temp !== null && (
              <button className="btn px-3" onClick={() => setChart({ unit: tab.unit, what: 'temp' })} title="Temperature history">
                <Thermometer size={18} /> {Math.round(tab.unit.temp)}°C
              </button>
            )}
          </div>
        )}
      </div>

      {!s.ams.length && tab.kind === 'ext' && (
        <div className="text-sm text-muted">No AMS reported by the printer - only the external spool holder.</div>
      )}

      <div className={`grid gap-3 ${slots.length > 1 ? 'grid-cols-2 lg:grid-cols-4' : 'grid-cols-1 sm:max-w-sm'}`}>
        {slots.map(({ ref, tray }) => (
          <SlotCard key={ref.label} s={s} ref_={ref} tray={tray} a={assignmentFor(snap.assignments, ref)} usedG={usedNow(snap, ref.loadId)} onOpen={() => setOpen(ref)} />
        ))}
      </div>

      <div className="flex flex-wrap justify-end gap-2">
        {printing && <span className="mr-auto self-center text-sm text-muted">Slot actions are off while printing.</span>}
        <button className="btn" disabled={!ready || printing || s.tray_now === 255} onClick={unload}>
          <Upload size={18} /> Unload filament
        </button>
        <button
          className="btn"
          disabled={!ready}
          onClick={async () => {
            await cmd('refresh')
            await cmd('refresh_inventory')
          }}
        >
          <RefreshCw size={18} /> Refresh
        </button>
      </div>

      <UsageList snap={snap} />

      {open && <SlotDialog snap={snap} ref_={open} onClose={() => setOpen(null)} />}
      {chart && <HistoryChart unit={chart.unit} what={chart.what} onClose={() => setChart(null)} />}
    </div>
  )
}

function SlotCard({ s, ref_, tray, a, usedG, onOpen }: { s: Status; ref_: SlotRef; tray: Tray | null; a: Assignment | null; usedG: number; onOpen: () => void }) {
  const st = stateText(s, ref_, tray)
  const used = st === 'IN USE'
  const has = !!tray?.has_filament
  const pct = remainPct(tray, a, usedG)
  return (
    <button
      onClick={onOpen}
      className={`card flex flex-col gap-3 p-4 text-left transition hover:border-accent/50 ${used ? 'border-accent/70 shadow-[0_0_22px_-10px_var(--color-accent)]' : ''}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-lg font-extrabold">{ref_.label}</span>
        <span
          className="rounded-md px-2 py-0.5 text-[11px] font-extrabold tracking-wide"
          style={{ background: used ? C.accent : st === 'EMPTY' ? 'var(--color-border)' : C.dim, color: st === 'EMPTY' ? C.muted : '#000' }}
        >
          {st}
        </span>
      </div>
      <div className="flex items-center gap-3">
        {has ? <Swatch color={tray!.color} size={52} /> : <div className="h-[52px] w-[52px] shrink-0 rounded-full border-2 border-dashed border-border" />}
        <div className="min-w-0">
          <div className="text-xl font-bold">{has ? tray!.type || '?' : 'Empty'}</div>
          <div className="truncate text-sm text-muted">{displayName(tray, a)}</div>
        </div>
      </div>
      <div className="mt-auto">
        <div className="h-2 overflow-hidden rounded-full bg-card2">
          {pct >= 0 && <div className="h-full rounded-full" style={{ width: `${Math.min(100, pct)}%`, background: pctColor(pct) }} />}
        </div>
        <div className="mt-1 flex justify-between text-xs text-muted">
          <span>
            {pct >= 0 ? `${pct}%` : has ? 'remaining unknown' : ''}
            <UsedNote used={usedG} />
          </span>
          {a && a.spool.label_weight > 0 && (
            <span>
              {usedG >= 0.5 && '~'}
              {Math.round(spoolLeft(a.spool, usedG))} g
            </span>
          )}
        </div>
      </div>
    </button>
  )
}

// ---------------------------------------------------------------- slot dialog

function Info({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="contents">
      <dt className="text-sm text-muted">{k}</dt>
      <dd className="text-sm font-semibold [overflow-wrap:anywhere]">{children}</dd>
    </div>
  )
}

export function SlotDialog({ snap, ref_, onClose }: { snap: Snapshot; ref_: SlotRef; onClose: () => void }) {
  const dlg = useDialogs()
  const [sub, setSub] = useState<'slot' | 'spool' | 'assign' | null>(null)
  const s = snap.status!
  const tray = slotsOf(ref_.amsId === EXT_AMS ? { kind: 'ext' } : { kind: 'unit', unit: s.ams.find((u) => u.id === ref_.amsId)! }, s).find(
    (x) => x.ref.label === ref_.label,
  )?.tray
  const a = assignmentFor(snap.assignments, ref_)
  const ext = ref_.amsId === EXT_AMS
  const ready = printerReady(snap)
  const printing = isPrinting(s)
  const off = !ready || printing
  const has = !!tray?.has_filament
  const run = async (name: string, args: Record<string, unknown> = {}, close = true) => {
    if ((await cmd(name, args)).ok && close) onClose()
  }

  if (!tray && !ext) {
    return (
      <Modal title={`Slot ${ref_.label}`} onClose={onClose}>
        <p className="text-muted">Slot no longer reported.</p>
      </Modal>
    )
  }

  const load = async () => {
    const other = s.tray_now !== 255 && s.tray_now !== ref_.loadId
    if (other) {
      const ok = await dlg.confirm({ title: 'Change filament', text: 'The printer will unload the current filament and load this slot.', ok: 'Change' })
      if (!ok) return
    }
    await run('ams_load', { tray: ref_.loadId })
  }
  const actions: { label: string; icon: ReactNode; onClick: () => void; disabled: boolean; tone?: string }[] = [
    { label: 'Load', icon: <Download size={18} />, onClick: load, disabled: off || !has || inUse(s, ref_), tone: 'btn-accent' },
    {
      label: 'Unload',
      icon: <Upload size={18} />,
      onClick: () => run('ams_unload'),
      disabled: off || !inUse(s, ref_),
    },
    { label: 'Edit slot', icon: <Pencil size={18} />, onClick: () => setSub('slot'), disabled: off },
  ]
  if (!ext)
    actions.push({
      label: 'Re-read RFID',
      icon: <ScanLine size={18} />,
      onClick: () => run('slot_rfid', { ams_id: ref_.amsId, tray_id: ref_.trayId }),
      disabled: off,
    })
  if (a) {
    actions.push({ label: 'Edit spool', icon: <Package size={18} />, onClick: () => setSub('spool'), disabled: !online(snap) })
    actions.push({
      label: 'Unassign spool',
      icon: <Unlink size={18} />,
      onClick: async () => {
        const ok = await dlg.confirm({ title: 'Unassign spool', text: 'Remove the inventory spool binding from this slot?', ok: 'Unassign', tone: 'warn' })
        if (ok) await run('spool_unassign', { ams_id: ref_.amsId, tray_id: ref_.trayId })
      },
      disabled: !online(snap),
    })
  } else {
    actions.push({ label: 'Assign spool', icon: <Link2 size={18} />, onClick: () => setSub('assign'), disabled: !online(snap) })
  }
  if (!ext)
    actions.push({
      label: 'Reset slot',
      icon: <RotateCcw size={18} />,
      onClick: async () => {
        const ok = await dlg.confirm({ title: 'Reset slot', text: 'Clear the filament information stored for this slot on the printer?', ok: 'Reset', tone: 'danger' })
        if (ok) await run('slot_reset', { ams_id: ref_.amsId, tray_id: ref_.trayId })
      },
      disabled: off,
      tone: 'btn-danger',
    })

  return (
    <>
      <Modal title={`Slot ${ref_.label}`} onClose={onClose} wide>
        <div className="flex flex-col gap-5 sm:flex-row">
          <div className="flex min-w-0 flex-col gap-3 sm:w-[46%]">
            <div className="flex items-center gap-4">
              {has ? <Swatch color={tray!.color} size={72} /> : <div className="h-[72px] w-[72px] rounded-full border-2 border-dashed border-border" />}
              <div className="min-w-0">
                <div className="text-2xl font-extrabold">{has ? tray!.type : 'Empty'}</div>
                <div className="truncate text-muted">{displayName(tray ?? null, a)}</div>
              </div>
            </div>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
              <Info k="Colour">{has ? tray!.color : '-'}</Info>
              <Info k="Remaining">{tray && tray.remain >= 0 ? `${tray.remain}%` : 'unknown'}</Info>
              {a && (
                <Info k="Weight">
                  {usedNow(snap, ref_.loadId) >= 0.5 && '~'}
                  {Math.round(spoolLeft(a.spool, usedNow(snap, ref_.loadId)))} g of {a.spool.label_weight} g
                  <UsedNote used={usedNow(snap, ref_.loadId)} long />
                </Info>
              )}
              {tray && (tray.temp_min > 0 || tray.temp_max > 0) && (
                <Info k="Nozzle">
                  {tray.temp_min} - {tray.temp_max}°C
                </Info>
              )}
              <Info k="State">{stateText(s, ref_, tray ?? null)}</Info>
              <Info k="Tag">{[tray?.rfid && 'RFID', tray?.info_idx, a && 'Spool'].filter(Boolean).join('  ') || '-'}</Info>
              {a && (
                <Info k="Spool">
                  #{a.spool.id} {a.spool.brand} {a.spool.material} {a.spool.subtype} {a.spool.color_name && `· ${a.spool.color_name}`}
                </Info>
              )}
            </dl>
          </div>
          <div className="grid flex-1 grid-cols-2 content-start gap-2">
            {actions.map((x) => (
              <button key={x.label} className={`${x.tone ?? 'btn'} h-14`} disabled={x.disabled} onClick={x.onClick}>
                {x.icon} {x.label}
              </button>
            ))}
            {printing && <p className="col-span-2 text-sm text-muted">Printer-side actions are off while printing.</p>}
          </div>
        </div>
      </Modal>
      {sub === 'slot' && <EditSlot ref_={ref_} tray={tray ?? null} onClose={() => setSub(null)} onSaved={onClose} />}
      {sub === 'spool' && a && <EditSpool spool={a.spool} onClose={() => setSub(null)} onSaved={onClose} />}
      {sub === 'assign' && <AssignSpool ref_={ref_} onClose={() => setSub(null)} onSaved={onClose} />}
    </>
  )
}

// ---------------------------------------------------------------- edit slot (printer-side)

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="label">{label}</span>
      {children}
    </label>
  )
}

function EditSlot({ ref_, tray, onClose, onSaved }: { ref_: SlotRef; tray: Tray | null; onClose: () => void; onSaved: () => void }) {
  const has = !!tray?.has_filament
  const origType = has ? tray!.type : ''
  const [type, setType] = useState(has && tray!.type ? tray!.type : 'PLA')
  const [sub, setSub] = useState(has ? tray!.sub_brands : '')
  const [color, setColor] = useState(has && tray!.color ? tray!.color : '#FFFFFF')
  const [tmin, setTmin] = useState(has && tray!.temp_min > 0 ? tray!.temp_min : 190)
  const [tmax, setTmax] = useState(has && tray!.temp_max > 0 ? tray!.temp_max : 230)
  const [busy, setBusy] = useState(false)
  const pick = (t: string) => {
    const m = MATERIALS.find((x) => x.type === t)
    setType(t)
    if (m) {
      setTmin(m.min)
      setTmax(m.max)
    }
    if (!sub || t.toLowerCase() !== origType.toLowerCase()) setSub(`Generic ${t}`)
  }
  const save = async () => {
    // Keep the slot's exact preset when the material is unchanged; otherwise Bambu's generic one.
    const idx =
      tray?.info_idx && type.toLowerCase() === origType.toLowerCase()
        ? tray.info_idx
        : MATERIALS.find((m) => m.type.toLowerCase() === type.toLowerCase())?.idx ?? 'GFL99'
    setBusy(true)
    const r = await cmd('slot_configure', {
      ams_id: ref_.amsId,
      tray_id: ref_.trayId,
      info_idx: idx,
      type,
      sub_brands: sub,
      color,
      temp_min: Math.min(tmin, tmax),
      temp_max: Math.max(tmin, tmax),
    })
    setBusy(false)
    if (r.ok) onSaved()
  }
  const types = MATERIALS.some((m) => m.type === type) ? MATERIALS.map((m) => m.type) : [type, ...MATERIALS.map((m) => m.type)]
  return (
    <Modal
      title={`Edit slot ${ref_.label}`}
      onClose={onClose}
      footer={
        <button className="btn-accent" disabled={busy} onClick={save}>
          {busy ? <Loader2 size={18} className="animate-spin" /> : <Save size={18} />} Save
        </button>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Material">
          <div className="flex flex-wrap gap-1.5">
            {types.map((t) => (
              <button
                key={t}
                onClick={() => pick(t)}
                className={`rounded-lg px-3 py-1.5 text-sm font-bold ${t === type ? 'bg-accent text-black' : 'bg-card2 text-dim hover:text-text'}`}
              >
                {t}
              </button>
            ))}
          </div>
        </Field>
        <Field label="Name">
          <input className="input" value={sub} maxLength={40} onChange={(e) => setSub(e.target.value)} placeholder="(none)" />
        </Field>
        <Field label="Colour">
          <ColorInput value={color} onChange={setColor} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Nozzle min °C">
            <input className="input" type="number" min={150} max={350} value={tmin} onChange={(e) => setTmin(Number(e.target.value))} />
          </Field>
          <Field label="Nozzle max °C">
            <input className="input" type="number" min={150} max={350} value={tmax} onChange={(e) => setTmax(Number(e.target.value))} />
          </Field>
        </div>
        <p className="text-xs text-muted">Saved on the printer (slot preset, colour and nozzle range).</p>
      </div>
    </Modal>
  )
}

export function ColorInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [text, setText] = useState(value)
  useEffect(() => setText(value), [value])
  return (
    <div className="flex items-center gap-3">
      <input
        type="color"
        value={value}
        onChange={(e) => onChange(e.target.value.toUpperCase())}
        className="h-11 w-16 cursor-pointer rounded-lg border border-border bg-card2 p-1"
      />
      <input
        className="input flex-1 font-mono uppercase"
        value={text}
        maxLength={7}
        onChange={(e) => {
          const v = e.target.value.toUpperCase()
          setText(v)
          if (/^#?[0-9A-F]{6}$/.test(v)) onChange(v.startsWith('#') ? v : `#${v}`)
        }}
      />
    </div>
  )
}

// ---------------------------------------------------------------- inventory spool: edit / assign

function EditSpool({ spool, onClose, onSaved }: { spool: Spool; onClose: () => void; onSaved: () => void }) {
  const [f, setF] = useState({
    material: spool.material,
    subtype: spool.subtype,
    brand: spool.brand,
    color_name: spool.color_name,
    color: spool.color || '#FFFFFF',
    label_weight: spool.label_weight,
    remaining: Math.round(spoolLeft(spool)),
  })
  const [busy, setBusy] = useState(false)
  const set = (k: keyof typeof f, v: string | number) => setF((o) => ({ ...o, [k]: v }))
  const save = async () => {
    // Only the changed fields (panel SpoolPatch).
    const body: Record<string, unknown> = { id: spool.id }
    for (const k of ['material', 'subtype', 'brand', 'color_name'] as const) if (f[k] !== spool[k]) body[k] = f[k]
    if (f.color.toUpperCase() !== (spool.color || '').toUpperCase()) body.color = f.color
    if (f.label_weight !== spool.label_weight) body.label_weight = f.label_weight
    if (f.remaining !== Math.round(spoolLeft(spool)) || f.label_weight !== spool.label_weight)
      body.weight_used = Math.max(0, f.label_weight - f.remaining)
    setBusy(true)
    const r = await cmd('spool_patch', body)
    setBusy(false)
    if (r.ok) onSaved()
  }
  return (
    <Modal
      title={`Edit spool #${spool.id}`}
      onClose={onClose}
      footer={
        <button className="btn-accent" disabled={busy} onClick={save}>
          {busy ? <Loader2 size={18} className="animate-spin" /> : <Save size={18} />} Save
        </button>
      }
    >
      <div className="grid grid-cols-2 gap-3">
        <Field label="Material">
          <input className="input" value={f.material} onChange={(e) => set('material', e.target.value)} />
        </Field>
        <Field label="Type / subtype">
          <input className="input" value={f.subtype} onChange={(e) => set('subtype', e.target.value)} />
        </Field>
        <Field label="Brand">
          <input className="input" value={f.brand} onChange={(e) => set('brand', e.target.value)} />
        </Field>
        <Field label="Colour name">
          <input className="input" value={f.color_name} onChange={(e) => set('color_name', e.target.value)} />
        </Field>
        <div className="col-span-2">
          <Field label="Colour">
            <ColorInput value={f.color} onChange={(v) => set('color', v)} />
          </Field>
        </div>
        <Field label="Label weight (g)">
          <input className="input" type="number" min={0} max={10000} value={f.label_weight} onChange={(e) => set('label_weight', Number(e.target.value))} />
        </Field>
        <Field label="Remaining (g)">
          <input className="input" type="number" min={0} max={10000} value={f.remaining} onChange={(e) => set('remaining', Number(e.target.value))} />
        </Field>
      </div>
    </Modal>
  )
}

interface SpoolRow extends Spool {
  assigned: boolean
}

function AssignSpool({ ref_, onClose, onSaved }: { ref_: SlotRef; onClose: () => void; onSaved: () => void }) {
  const [data, setData] = useState<{ ok: boolean; error: string; spools: SpoolRow[] } | null>(null)
  const [q, setQ] = useState('')
  const [busy, setBusy] = useState(-1)
  useEffect(() => {
    api<{ ok: boolean; error: string; spools: SpoolRow[] }>('/api/inventory/spools')
      .then(setData)
      .catch((e: ApiError) => setData({ ok: false, error: e.message, spools: [] }))
  }, [])
  const list = useMemo(() => {
    const words = q.toLowerCase().split(/\s+/).filter(Boolean)
    return (data?.spools ?? []).filter((sp) => {
      const hay = `${sp.brand} ${sp.material} ${sp.subtype} ${sp.color_name} #${sp.id}`.toLowerCase()
      return words.every((w) => hay.includes(w))
    })
  }, [data, q])
  const assign = async (sp: SpoolRow) => {
    setBusy(sp.id)
    const r = await cmd('spool_assign', { spool_id: sp.id, ams_id: ref_.amsId, tray_id: ref_.trayId })
    setBusy(-1)
    if (r.ok) onSaved()
  }
  return (
    <Modal title={`Assign spool to ${ref_.label}`} onClose={onClose} wide>
      <div className="flex flex-col gap-3">
        <div className="relative">
          <Search size={16} className="absolute top-1/2 left-3 -translate-y-1/2 text-muted" />
          <input className="input w-full pl-9" placeholder="Search brand, material, colour..." value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        {!data && (
          <p className="flex items-center gap-2 text-muted">
            <Loader2 size={16} className="animate-spin" /> Loading inventory...
          </p>
        )}
        {data && !data.ok && <p className="text-error">{data.error}</p>}
        {data?.ok && !list.length && <p className="text-muted">No spools{q && ' match'}.</p>}
        <div className="flex max-h-[55vh] flex-col gap-1.5 overflow-y-auto">
          {list.map((sp) => (
            <button
              key={sp.id}
              disabled={busy >= 0}
              onClick={() => assign(sp)}
              className="flex items-center gap-3 rounded-xl bg-card px-3 py-2.5 text-left transition hover:bg-card2"
            >
              <Swatch color={sp.color} size={30} />
              <div className="min-w-0 flex-1">
                <div className="truncate font-semibold">
                  {sp.brand} {sp.material} {sp.subtype}
                  {sp.color_name && <span className="text-muted"> · {sp.color_name}</span>}
                </div>
                <div className="text-xs text-muted">
                  #{sp.id}
                  {sp.label_weight > 0 && ` · ${Math.round(spoolLeft(sp))} g of ${sp.label_weight} g`}
                  {sp.assigned && <span className="text-warn"> · in another slot</span>}
                </div>
              </div>
              {busy === sp.id && <Loader2 size={18} className="animate-spin text-accent" />}
            </button>
          ))}
        </div>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------- humidity / temperature history

interface AmsHist {
  ok: boolean
  error: string
  hours: number
  start: number
  end: number
  samples: number
  hum: (number | null)[]
  temp: (number | null)[]
  min_humidity: number | null
  max_humidity: number | null
  avg_humidity: number | null
  min_temperature: number | null
  max_temperature: number | null
  avg_temperature: number | null
}

const RANGES: [number, string][] = [
  [6, '6 h'],
  [24, '24 h'],
  [72, '3 d'],
  [168, '7 d'],
]

export function HistoryChart({ unit, what: initial, onClose }: { unit: AmsUnit; what: 'hum' | 'temp'; onClose: () => void }) {
  const [what, setWhat] = useState(initial)
  const [hours, setHours] = useState(24)
  const [d, setD] = useState<AmsHist | null>(null)
  useEffect(() => {
    let alive = true
    setD(null)
    api<AmsHist>(`/api/ams/history?ams=${unit.id}&hours=${hours}`)
      .then((x) => alive && setD(x))
      .catch((e: ApiError) => alive && setD({ ok: false, error: e.message } as AmsHist))
    return () => {
      alive = false
    }
  }, [unit.id, hours])
  const vals = d?.ok ? (what === 'hum' ? d.hum : d.temp) : []
  const color = what === 'hum' ? C.info : C.heat
  const unitTxt = what === 'hum' ? '%' : '°C'
  const nums = vals.filter((v): v is number => v !== null)
  const lo = nums.length ? Math.floor(Math.min(...nums) / 5) * 5 - (what === 'hum' ? 5 : 2) : 0
  const hi = nums.length ? Math.ceil(Math.max(...nums) / 5) * 5 + (what === 'hum' ? 5 : 2) : 100
  const ymin = Math.max(0, lo)
  const ymax = Math.max(hi, ymin + 10)
  const W = 600
  const H = 220
  const x = (i: number) => (i / Math.max(1, vals.length - 1)) * W
  const y = (v: number) => H - ((v - ymin) / (ymax - ymin)) * H
  // Short gaps (a missed sample or two) are bridged; longer ones break the line.
  let path = ''
  let last = -99
  vals.forEach((v, i) => {
    if (v === null) return
    path += `${i - last <= 3 ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`
    last = i
  })
  const st =
    d?.ok &&
    (what === 'hum' ? [d.min_humidity, d.avg_humidity, d.max_humidity] : [d.min_temperature, d.avg_temperature, d.max_temperature])
  const warnY = what === 'hum' && 40 > ymin && 40 < ymax ? y(40) : null
  const fmtT = (t: number) =>
    new Date(t * 1000).toLocaleString('en-GB', hours <= 24 ? { hour: '2-digit', minute: '2-digit' } : { weekday: 'short', hour: '2-digit', minute: '2-digit' })
  return (
    <Modal title={`${unitName(unit)} - ${what === 'hum' ? 'humidity' : 'temperature'}`} onClose={onClose} wide>
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex gap-1 rounded-xl bg-card p-1">
          {(['hum', 'temp'] as const).map((w) => (
            <button
              key={w}
              onClick={() => setWhat(w)}
              className={`rounded-lg px-3 py-1.5 text-sm font-bold ${what === w ? 'bg-card2 text-accent' : 'text-muted'}`}
            >
              {w === 'hum' ? 'Humidity' : 'Temperature'}
            </button>
          ))}
        </div>
        <div className="ml-auto flex gap-1 rounded-xl bg-card p-1">
          {RANGES.map(([h, l]) => (
            <button key={h} onClick={() => setHours(h)} className={`rounded-lg px-3 py-1.5 text-sm font-bold ${hours === h ? 'bg-card2 text-accent' : 'text-muted'}`}>
              {l}
            </button>
          ))}
        </div>
      </div>
      <div className="mt-4">
        {!d && (
          <p className="flex items-center gap-2 text-muted">
            <Loader2 size={16} className="animate-spin" /> Loading...
          </p>
        )}
        {d && !d.ok && <p className="text-error">{d.error}</p>}
        {d?.ok && !nums.length && <p className="text-muted">No data in this period.</p>}
        {d?.ok && nums.length > 0 && (
          <>
            {st && (
              <div className="mb-3 flex flex-wrap gap-x-6 gap-y-1 text-sm font-semibold">
                {(['Min', 'Avg', 'Max'] as const).map((k, i) => (
                  <span key={k}>
                    <span className="text-muted">{k} </span>
                    <span style={{ color }}>{st[i] !== null ? `${st[i]!.toFixed(1)}${unitTxt}` : '--'}</span>
                  </span>
                ))}
              </div>
            )}
            <svg viewBox={`0 0 ${W} ${H + 22}`} className="w-full">
              {[0, 0.5, 1].map((f) => {
                const v = ymin + (ymax - ymin) * f
                return (
                  <g key={f}>
                    <line x1="0" x2={W} y1={y(v)} y2={y(v)} stroke={C.card2} />
                    <text x="4" y={f === 1 ? y(v) + 13 : y(v) - 4} fill={C.muted} fontSize="11">
                      {Math.round(v)}
                      {unitTxt}
                    </text>
                  </g>
                )
              })}
              {warnY !== null && <line x1="0" x2={W} y1={warnY} y2={warnY} stroke={C.warn} strokeDasharray="6 5" strokeWidth="1.5" />}
              <path d={path} fill="none" stroke={color} strokeWidth="2.5" strokeLinejoin="round" style={{ filter: `drop-shadow(0 0 3px ${color})` }} />
              <text x="2" y={H + 17} fill={C.muted} fontSize="11">
                {fmtT(d.start)}
              </text>
              <text x={W - 2} y={H + 17} fill={C.muted} fontSize="11" textAnchor="end">
                now
              </text>
            </svg>
            <p className="mt-2 text-xs text-muted">
              {d.samples} samples (one every 5 min).{what === 'hum' && ' Dashed line: 40 % - above it, dry the filament or replace the desiccant.'}
            </p>
          </>
        )}
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------- compact AMS card (desktop HOME)

/** Black or white text on a filament colour. */
function onColor(hex: string): string {
  const h = (hex || '#000000').replace('#', '')
  const [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16) || 0)
  return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? '#000' : '#fff'
}

/** Details of a tray on hover (desktop): drawn on <body> so the scrolling column does not clip it. */
function TrayHover({
  s,
  r,
  tray,
  a,
  at,
  runout,
  used = 0,
  need = 0,
}: {
  s: Status
  r: SlotRef
  tray: Tray | null
  a: Assignment | null
  at: DOMRect
  runout?: string
  used?: number
  need?: number
}) {
  const has = !!tray?.has_filament
  const W = 270
  const left = Math.min(Math.max(8, at.left + at.width / 2 - W / 2), window.innerWidth - W - 8)
  const below = at.bottom + 280 < window.innerHeight
  const sp = a?.spool
  const rows: [string, ReactNode][] = []
  let spoolGrid: ReactNode = null
  let shortBy = 0
  if (has) {
    rows.push(['Colour', <span className="flex items-center gap-1.5">{tray!.color}{sp?.color_name && <span className="text-muted">· {sp.color_name}</span>}</span>])
    if (sp && sp.label_weight > 0 && (need > 0 || used >= 0.5)) {
      // During a print: the spool at the start, now, and at the end of the print (estimates).
      const start = spoolLeft(sp)
      const now = spoolLeft(sp, used)
      const end = start - (need > 0 ? need : used)
      const cell = (label: string, g: number, est: boolean) => (
        <div className="rounded-lg bg-card px-2 py-1.5 text-center">
          <div className="text-[10px] font-bold tracking-wider text-muted uppercase">{label}</div>
          <div className="text-sm font-extrabold tabular-nums" style={{ color: g < 0 ? C.error : pctColor((100 * Math.max(0, g)) / sp.label_weight) }}>
            {est && '~'}
            {Math.round(g)} g
          </div>
        </div>
      )
      rows.push([
        'This print',
        <span className="text-info">
          {need > 0 ? `needs ${need.toFixed(0)} g, ${used.toFixed(0)} g used so far` : `${used.toFixed(0)} g used so far`}
        </span>,
      ])
      spoolGrid = (
        <div className="mb-2 grid grid-cols-3 gap-1.5">
          {cell('Start', start, false)}
          {cell('Now', now, true)}
          {cell(need > 0 ? 'At the end' : 'Now - used', end, true)}
        </div>
      )
      if (end < 0) shortBy = -end
    } else if (sp && sp.label_weight > 0) {
      const left_ = spoolLeft(sp, used)
      rows.push([
        'Spool left',
        <span style={{ color: pctColor((100 * left_) / sp.label_weight) }}>
          {used >= 0.5 && '~'}
          {Math.round(left_)} g of {sp.label_weight} g
        </span>,
      ])
    }
    rows.push(['AMS estimate', tray!.remain >= 0 ? `${tray!.remain} %` : 'unknown'])
    if (sp) rows.push(['Spool', `#${sp.id} ${sp.brand} ${sp.material} ${sp.subtype}`.trim()])
    if (tray!.temp_min > 0 || tray!.temp_max > 0) rows.push(['Nozzle', `${tray!.temp_min} - ${tray!.temp_max} °C`])
    if (tray!.k > 0) rows.push(['Flow (K)', tray!.k.toFixed(3)])
    rows.push(['Tag', [tray!.rfid && 'RFID', tray!.info_idx].filter(Boolean).join('  ') || '-'])
  }
  rows.push(['State', stateText(s, r, tray)])
  return createPortal(
    <div
      className="pointer-events-none fixed z-[80] rounded-xl border border-border bg-panel/95 p-3 text-sm shadow-[0_12px_40px_-12px_#000] backdrop-blur"
      style={{ left, width: W, top: below ? at.bottom + 8 : at.top - 8, transform: below ? undefined : 'translateY(-100%)' }}
    >
      <div className="mb-2 flex items-center gap-2.5">
        {has ? <Swatch color={tray!.color} size={26} /> : <div className="h-[26px] w-[26px] rounded-full border-2 border-dashed border-border" />}
        <div className="min-w-0">
          <div className="font-extrabold">
            {r.label} · {has ? tray!.type || '?' : 'Empty'}
          </div>
          <div className="truncate text-xs text-muted">{displayName(tray, a)}</div>
        </div>
      </div>
      {spoolGrid}
      {shortBy > 0 && !runout && (
        <div className="mb-2 rounded-lg bg-error/15 px-2 py-1.5 text-xs font-semibold text-error">Not enough for this print: ~{Math.round(shortBy)} g short</div>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-xs text-muted">{k}</dt>
            <dd className="text-xs font-semibold [overflow-wrap:anywhere]">{v}</dd>
          </div>
        ))}
      </dl>
      {runout && <div className="mt-2 rounded-lg bg-error/15 px-2 py-1.5 text-xs font-semibold text-error">{runout}</div>}
      <div className="mt-2 text-[11px] text-muted">Click for load / edit / spool</div>
    </div>,
    document.body,
  )
}

function MiniTile({
  s,
  r,
  tray,
  a,
  onOpen,
  runout,
  usedG = 0,
  needG = 0,
}: {
  s: Status
  r: SlotRef
  tray: Tray | null
  a: Assignment | null
  onOpen: () => void
  runout?: string
  usedG?: number
  needG?: number
}) {
  const has = !!tray?.has_filament
  const used = inUse(s, r)
  const pct = remainPct(tray, a, usedG)
  const fg = has ? onColor(tray!.color) : C.muted
  const [hover, setHover] = useState<DOMRect | null>(null)
  return (
    <button
      onClick={onOpen}
      onMouseEnter={(e) => setHover(e.currentTarget.getBoundingClientRect())}
      onMouseLeave={() => setHover(null)}
      className="group flex h-full w-full min-w-0 flex-col items-center gap-1.5"
    >
      <span className={`text-[11px] font-extrabold ${used ? 'text-accent' : 'text-muted'}`}>{r.label}</span>
      <div
        className={`@container relative flex min-h-[clamp(64px,12vh,150px)] w-full flex-1 flex-col items-center justify-center gap-0.5 overflow-hidden rounded-lg border-2 px-1 text-center transition group-hover:brightness-110 ${
          used ? 'border-accent shadow-[0_0_16px_-4px_var(--color-accent)]' : has ? 'border-transparent' : 'border-dashed border-border'
        }`}
        style={{ background: has ? tray!.color : 'transparent', color: fg }}
      >
        <span className="text-sm leading-tight font-extrabold @[90px]:text-base">{has ? tray!.type || '?' : ''}</span>
        {has && pct >= 0 && <span className="text-[11px] font-semibold opacity-80 @[90px]:text-sm">{pct}%</span>}
        {/* Room for more on a tall tile: the spool's name and grams left. */}
        {has && (
          <span className="tile-more mt-1 hidden max-w-full truncate text-[11px] font-semibold opacity-75">{displayName(tray, a)}</span>
        )}
        {has && a && a.spool.label_weight > 0 && (
          <span className="tile-more hidden text-[11px] font-semibold opacity-75 tabular-nums">
            {usedG >= 0.5 && '~'}
            {Math.round(spoolLeft(a.spool, usedG))} g
          </span>
        )}
        {!has && <span className="text-xs">Empty</span>}
      </div>
      <div className="h-1 w-full overflow-hidden rounded-full bg-card2">
        {pct >= 0 && <div className="h-full rounded-full" style={{ width: `${Math.min(100, pct)}%`, background: pctColor(pct) }} />}
      </div>
      {hover && <TrayHover s={s} r={r} tray={tray} a={a} at={hover} runout={runout} used={usedG} need={needG} />}
    </button>
  )
}

export function AmsMini({ snap, goAms, fill }: { snap: Snapshot; goAms: () => void; fill?: boolean }) {
  const dlg = useDialogs()
  const s = snap.status!
  const [unitIdx, setUnitIdx] = useState(0)
  const [open, setOpen] = useState<SlotRef | null>(null)
  const [chart, setChart] = useState<{ unit: AmsUnit; what: 'hum' | 'temp' } | null>(null)
  const unit = s.ams[Math.min(unitIdx, s.ams.length - 1)]
  const slots = unit ? slotsOf({ kind: 'unit', unit }, s) : []
  const ext = slotsOf({ kind: 'ext' }, s)[0]
  // The external spool can be hidden here (Settings-backed, so every device agrees); while it
  // feeds the printer it is shown anyway.
  const hideExt = snap.settings.hide_external && s.tray_now !== 254
  const ro = snap.alerts?.runout
  const runoutFor = (r: SlotRef) => (ro && ro.short && ro.tray === r.loadId ? ro.text : '')
  const toggleExt = () => api('/api/settings', { method: 'PATCH', body: { hide_external: !snap.settings.hide_external } }).catch(() => {})
  const ready = printerReady(snap)
  const printing = isPrinting(s)
  const unload = async () => {
    const ok = await dlg.confirm({ title: 'Unload filament', text: 'Retract the currently loaded filament back into its slot?', ok: 'Unload' })
    if (ok) await cmd('ams_unload')
  }
  return (
    <div className={`card flex flex-col gap-2.5 p-3.5 ${fill ? 'ams-fill flex-1' : ''}`}>
      <div className="flex items-center gap-2">
        <span className="section-title mb-0!">Filament</span>
        {s.ams.length > 1 &&
          s.ams.map((u, i) => (
            <button
              key={u.id}
              onClick={() => setUnitIdx(i)}
              className={`rounded-md px-2 py-0.5 text-xs font-bold ${u === unit ? 'bg-card2 text-accent' : 'text-muted hover:text-text'}`}
            >
              {unitName(u)}
            </button>
          ))}
        <div className="flex-1" />
        <button
          className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-bold text-muted hover:text-text"
          onClick={toggleExt}
          title={snap.settings.hide_external ? 'Show the external spool' : 'Hide the external spool'}
        >
          {snap.settings.hide_external ? <Eye size={14} /> : <EyeOff size={14} />} Ext
        </button>
        {unit && (
          <>
            <button className="flex items-center gap-1 text-xs font-bold hover:brightness-125" style={{ color: unit.humidity > 40 ? C.info : C.accent }} onClick={() => setChart({ unit, what: 'hum' })} title="Humidity history">
              <Droplets size={14} /> {unit.humidity}%
            </button>
            {unit.temp !== null && (
              <button className="flex items-center gap-1 text-xs font-bold text-accent hover:brightness-125" onClick={() => setChart({ unit, what: 'temp' })} title="Temperature history">
                <Thermometer size={14} /> {unit.temp.toFixed(1)}°C
              </button>
            )}
          </>
        )}
      </div>
      <div className={`grid min-h-0 flex-1 gap-4 ${hideExt ? 'grid-cols-1' : 'grid-cols-[4fr_1fr]'}`}>
        {unit ? (
          <div className="grid grid-cols-4 gap-2.5">
            {slots.map(({ ref, tray }) => (
              <MiniTile key={ref.label} s={s} r={ref} tray={tray} a={assignmentFor(snap.assignments, ref)} onOpen={() => setOpen(ref)} runout={runoutFor(ref)} usedG={usedNow(snap, ref.loadId)} needG={needOf(snap, ref.loadId)} />
            ))}
          </div>
        ) : (
          <div className="flex flex-1 items-center text-sm text-muted">No AMS reported.</div>
        )}
        {!hideExt && (
          <div className="border-l border-border/60 pl-4">
            <MiniTile s={s} r={ext.ref} tray={ext.tray} a={assignmentFor(snap.assignments, ext.ref)} onOpen={() => setOpen(ext.ref)} runout={runoutFor(ext.ref)} usedG={usedNow(snap, ext.ref.loadId)} needG={needOf(snap, ext.ref.loadId)} />
          </div>
        )}
      </div>
      <div className="flex gap-2">
        <button className="btn h-9 flex-1 text-sm" disabled={!ready || printing || s.tray_now === 255} onClick={unload}>
          <Upload size={16} /> Unload
        </button>
        <button className="btn h-9 flex-1 text-sm" onClick={goAms}>
          <Boxes size={16} /> AMS page
        </button>
      </div>
      {open && <SlotDialog snap={snap} ref_={open} onClose={() => setOpen(null)} />}
      {chart && <HistoryChart unit={chart.unit} what={chart.what} onClose={() => setChart(null)} />}
    </div>
  )
}
