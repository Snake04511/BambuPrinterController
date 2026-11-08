// AMS mapping + filament forecast (panel AmsMapping.h, FilesPage.cpp forecast; server: backend/app/forecast.py).
import type { Assignment, Spool, Status, Tray } from './types'

export const MAP_UNUSED = -1
export const MAP_MISSING = -2
export const MAP_EXTERNAL = 254

export interface PlateFilament {
  slot: number // 1-based filament slot of the file
  color: string
  type: string
  grams: number
  meters: number
}

export interface LoadedTray {
  id: number // global tray id (ams*4+slot, AMS-HT id, 254 external)
  label: string
  tray: Tray
}

/** "A1", "B3", "HT1", "Ext". */
export function trayLabel(id: number): string {
  if (id === MAP_EXTERNAL) return 'Ext'
  if (id >= 128) return `HT${id - 127}`
  if (id >= 0) return `${String.fromCharCode(65 + Math.floor(id / 4))}${(id % 4) + 1}`
  return '-'
}

export function loadedTrays(s: Status): LoadedTray[] {
  const out: LoadedTray[] = []
  for (const unit of s.ams) {
    unit.trays.forEach((t, slot) => {
      if (!t || !t.has_filament) return
      const id = unit.id >= 128 ? unit.id : unit.id * 4 + slot
      out.push({ id, label: trayLabel(id), tray: t })
    })
  }
  const ext = s.ext[0]
  if (ext && ext.has_filament) out.push({ id: MAP_EXTERNAL, label: 'Ext', tray: ext })
  return out
}

function rgb(c: string): [number, number, number] {
  const h = (c || '#000000').replace('#', '')
  return [parseInt(h.slice(0, 2), 16) || 0, parseInt(h.slice(2, 4), 16) || 0, parseInt(h.slice(4, 6), 16) || 0]
}

function colorDistance(a: string, b: string): number {
  const [r1, g1, b1] = rgb(a)
  const [r2, g2, b2] = rgb(b)
  return (r1 - r2) ** 2 + (g1 - g2) ** 2 + (b1 - b2) ** 2
}

const sameType = (a: string, b: string) => !!a && !!b && a.toLowerCase() === b.toLowerCase()

/**
 * Exact type + exact colour, then type + closest colour; every
 * AMS tray at most once; the external spool when nothing in the AMS matches.
 * Returns the tray per slot (index = slot - 1).
 */
export function computeAmsMap(s: Status, filaments: PlateFilament[]): number[] {
  const count = Math.max(0, ...filaments.map((f) => f.slot))
  const map = Array<number>(Math.min(count, 16)).fill(MAP_UNUSED)
  const cands = loadedTrays(s)
    .filter((c) => c.id !== MAP_EXTERNAL)
    .map((c) => ({ ...c, used: false }))
  const ext = s.ext[0]
  for (const f of filaments) {
    if (f.slot < 1 || f.slot > 16) continue
    let best = cands.find((c) => !c.used && sameType(c.tray.type, f.type) && c.tray.color.toUpperCase() === f.color.toUpperCase())
    if (!best) {
      let d = Infinity
      for (const c of cands) {
        if (c.used || !sameType(c.tray.type, f.type)) continue
        const dist = colorDistance(c.tray.color, f.color)
        if (dist < d) {
          d = dist
          best = c
        }
      }
    }
    if (best) {
      best.used = true
      map[f.slot - 1] = best.id
    } else if (ext && ext.has_filament && sameType(ext.type, f.type)) {
      map[f.slot - 1] = MAP_EXTERNAL
    } else {
      map[f.slot - 1] = MAP_MISSING
    }
  }
  return map
}

/** Auto map with the user's choices on top (overrides[slot-1] = tray id, undefined = auto). */
export function applyOverrides(auto: number[], overrides: Record<number, number>): number[] {
  return auto.map((t, i) => (overrides[i] !== undefined ? overrides[i] : t))
}

export function trayById(s: Status, id: number): Tray | null {
  return loadedTrays(s).find((t) => t.id === id)?.tray ?? null
}

/** Inventory spool bound to a tray (external: ams 255 / tray 0, AMS-HT: its id / 0). */
export function spoolFor(assignments: Assignment[], id: number): Spool | null {
  let ams: number, tray: number
  if (id === MAP_EXTERNAL) [ams, tray] = [255, 0]
  else if (id >= 128) [ams, tray] = [id, 0]
  else [ams, tray] = [Math.floor(id / 4), id % 4]
  return assignments.find((a) => a.ams_id === ams && a.tray_id === tray)?.spool ?? null
}

export type ForecastState = 'missing' | 'no_usage' | 'no_spool' | 'short' | 'low' | 'enough'

export interface ForecastRow {
  id: number // tray id, or MAP_MISSING for an unmapped filament
  label: string
  tray: Tray | null
  spool: Spool | null
  need: number | null // grams, null = unknown
  now: number | null
  after: number | null
  state: ForecastState
  auto: boolean
  fileColor: string
}

export const FORECAST_TEXT: Record<ForecastState, string> = {
  missing: 'Not loaded',
  no_usage: 'Filament usage not available',
  no_spool: 'Spool weight unknown',
  short: 'NOT ENOUGH',
  low: 'Low remaining',
  enough: '✓ Enough',
}

/** Per tray: need = grams of every file filament mapped to it, now = spool label - used. */
export function forecast(
  s: Status,
  assignments: Assignment[],
  filaments: PlateFilament[],
  map: number[],
  overrides: Record<number, number>,
  live: Record<string, number> = {}, // grams the running print used so far per tray (not booked yet)
): ForecastRow[] {
  const rows = new Map<number, ForecastRow>()
  const missing: ForecastRow[] = []
  for (const f of filaments) {
    const id = map[f.slot - 1]
    if (id === undefined || id === MAP_UNUSED) continue
    if (id === MAP_MISSING) {
      missing.push({
        id,
        label: `#${f.slot}`,
        tray: null,
        spool: null,
        need: f.grams > 0 ? f.grams : null,
        now: null,
        after: null,
        state: 'missing',
        auto: true,
        fileColor: f.color,
      })
      continue
    }
    let r = rows.get(id)
    if (!r) {
      const spool = spoolFor(assignments, id)
      r = {
        id,
        label: trayLabel(id),
        tray: trayById(s, id),
        spool,
        need: 0,
        now: spool && spool.label_weight > 0 ? Math.max(0, spool.label_weight - spool.weight_used - (live[String(id)] ?? 0)) : null,
        after: null,
        state: 'enough',
        auto: overrides[f.slot - 1] === undefined,
        fileColor: f.color,
      }
      rows.set(id, r)
    }
    if (overrides[f.slot - 1] !== undefined) r.auto = false
    if (f.grams > 0 && r.need !== null) r.need += f.grams
    else r.need = null
  }
  for (const r of rows.values()) {
    if (!r.tray) r.state = 'missing'
    else if (r.need === null || r.need <= 0) r.state = 'no_usage'
    else if (r.now === null) r.state = 'no_spool'
    else {
      r.after = r.now - r.need
      const label = r.spool?.label_weight ?? 0
      r.state = r.after < 0 ? 'short' : r.after < 50 || (label > 0 && r.after < label * 0.1) ? 'low' : 'enough'
    }
  }
  return [...rows.values(), ...missing]
}
