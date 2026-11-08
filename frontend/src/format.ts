// Formatting rules of the panel (src/ui/App.cpp, HomePage.cpp, Theme.h).
import type { Snapshot, Status, Tray } from './types'

export const ACTIVE_STATES = new Set(['RUNNING', 'PAUSE', 'PREPARE', 'SLICING'])
export const SPEED_NAMES: Record<number, string> = { 1: 'Silent', 2: 'Standard', 3: 'Sport', 4: 'Ludicrous' }

export const C = {
  accent: '#38FF9C',
  warn: '#FFB020',
  error: '#FF5252',
  info: '#3D9BFF',
  heat: '#FF7A45',
  cool: '#4FB3FF',
  text: '#ECEEF1',
  muted: '#8E96A3',
  dim: '#B8BEC8',
  card2: '#2A2F3A',
}

export function isPrinting(s: Status | null | undefined): boolean {
  return !!s && ACTIVE_STATES.has(s.state)
}

export function online(snap: Snapshot | null): boolean {
  return !!snap && snap.link === 'online' && !!snap.status
}

export function printerReady(snap: Snapshot | null): boolean {
  return online(snap) && !!snap!.status!.connected
}

export function hasJob(s: Status): boolean {
  return isPrinting(s) || s.state === 'FINISH' || s.state === 'FAILED'
}

export function stateText(s: Status | null | undefined): string {
  if (!s || !s.connected) return 'OFFLINE'
  switch (s.state) {
    case 'RUNNING':
      return 'PRINTING'
    case 'PREPARE':
      return 'PREPARING'
    case 'PAUSE':
      return 'PAUSED'
    case 'FINISH':
      return 'FINISHED'
    case 'FAILED':
      return 'FAILED'
    case 'SLICING':
      return 'SLICING'
    case '':
    case 'IDLE':
      return 'IDLE'
    default:
      return s.state
  }
}

export function stateColor(s: Status | null | undefined): string {
  if (!s || !s.connected) return C.muted
  switch (s.state) {
    case 'RUNNING':
    case 'PREPARE':
      return C.accent
    case 'PAUSE':
      return C.warn
    case 'FINISH':
      return C.info
    case 'FAILED':
      return C.error
    default:
      return C.dim
  }
}

/** Off -> text, cooling to a lower target -> cool, heating / holding -> heat. */
export function tempColor(current: number, target: number): string {
  if (target <= 0) return C.text
  return target < current - 1 ? C.cool : C.heat
}

const pad = (n: number) => String(n).padStart(2, '0')

export function clock(d = new Date()): string {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`
}

/** "HH:MM" today, else "Tue 02:10". null when unknown. */
export function formatEta(remainingMin: number): string | null {
  if (remainingMin < 0) return null
  const now = new Date()
  const t = new Date(now.getTime() + remainingMin * 60000)
  const same = t.toDateString() === now.toDateString()
  return same ? clock(t) : `${t.toLocaleDateString('en-GB', { weekday: 'short' })} ${clock(t)}`
}

export function formatDuration(min: number): string {
  if (min < 0) return '--'
  if (min >= 60) return `${Math.floor(min / 60)}h ${pad(Math.round(min % 60))}m`
  return `${Math.round(min)} min`
}

/** Tray currently feeding (tray_now): 254 = external, else ams * 4 + slot. */
export function activeTray(s: Status): Tray | null {
  const t = s.tray_now
  if (t === 255 || t < 0) return null
  if (t === 254) return s.ext[0] ?? null
  const unit = s.ams.find((u) => u.id === Math.floor(t / 4)) ?? s.ams[Math.floor(t / 4)]
  return unit?.trays[t % 4] ?? null
}

export function plateClearNeeded(snap: Snapshot): boolean {
  const s = snap.status
  const q = snap.plate_seq
  return !!s && !!q && (q.state === 'waiting' || q.state === 'paused') && !!q.next && !isPrinting(s)
}

export function untilText(m: { until: number; by_days: boolean }): string {
  const unit = m.by_days ? 'd' : 'h'
  return m.until < 0 ? `Overdue by ${Math.abs(m.until).toFixed(0)} ${unit}` : `Due in ${m.until.toFixed(0)} ${unit}`
}

export function unseenNotifs(snap: Snapshot): number {
  return (snap.notifications?.items ?? []).filter((n) => n.id > snap.notif_seen_id).length
}
