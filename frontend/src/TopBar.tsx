// Top bar (panel src/ui/App.cpp buildTopBar / updateTopBar) and its dialogs.
import { Bell, Droplet, Droplets, Eye, Loader2, TriangleAlert, Wifi, Wrench } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { api, cmd } from './api'
import { C, clock, formatEta, isPrinting, online, printerReady, stateColor, stateText, unseenNotifs, untilText } from './format'
import type { Snapshot } from './types'
import { HumidityDialog, LowFilamentDialog } from './PrintScreens'
import { ObicoBody, type ObicoRec } from './pages/Stats'
import { TimelapseButton, TimelapsePlayer } from './timelapse'
import { Modal, useDialogs } from './ui'

type DialogKind = 'hms' | 'maint' | 'notif' | 'obico' | 'low' | 'humid' | null

function useClock(): string {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = window.setInterval(() => setNow(new Date()), 5000)
    return () => window.clearInterval(t)
  }, [])
  return clock(now)
}

function IconBtn({
  bg,
  fg = '#000',
  onClick,
  children,
  title,
}: {
  bg: string
  fg?: string
  onClick: () => void
  children: ReactNode
  title: string
}) {
  return (
    <button
      title={title}
      onClick={onClick}
      className="flex h-9 min-w-9 shrink-0 items-center justify-center gap-1 rounded-lg px-2 text-xs font-bold transition hover:brightness-110"
      style={{ background: bg, color: fg }}
    >
      {children}
    </button>
  )
}

/** Obico eye look (App.cpp obicoLook). */
export function obicoLook(snap: Snapshot): { color: string; alert: boolean } {
  const o = snap.obico!
  const s = snap.status
  const printing = isPrinting(s)
  if (!o.running || o.error) return { color: C.error, alert: true }
  if (!printing) return { color: C.card2, alert: false }
  if (s && !s.light) return { color: C.warn, alert: true }
  if (o.cls === 'failure') return { color: C.error, alert: true }
  if (o.cls === 'warning') return { color: C.warn, alert: false }
  if (o.cls === 'safe') return { color: C.accent, alert: false }
  return { color: C.info, alert: false }
}

export function TopBar({ snap }: { snap: Snapshot | null }) {
  const time = useClock()
  const [dialog, setDialog] = useState<DialogKind>(null)
  const on = online(snap)
  const s = snap?.status
  const printing = isPrinting(s)

  let linkBadge: ReactNode = null
  if (snap && snap.link !== 'online') {
    const red = snap.link === 'offline'
    linkBadge = (
      <span className={`flex min-w-0 items-center gap-1 truncate text-xs font-semibold ${red ? 'text-error' : 'text-warn'}`}>
        {red ? <TriangleAlert size={14} /> : <Wifi size={14} />}
        <span className="truncate">{snap.link_text}</span>
      </span>
    )
  }

  const eta = s && printing ? formatEta(s.remaining_min) : null
  const maintCount = snap?.maintenance ? snap.maintenance.items.length : 0
  const maintDue = snap?.maintenance?.items.some((m) => m.due)
  const unseen = snap ? unseenNotifs(snap) : 0
  // Low filament: amber, red while the printer feeds from a low tray or the spool runs out during this print.
  const al = snap?.alerts
  const runsOut = !!al?.runout?.short
  const lowCount = al?.low.length ?? 0
  const lowActive = runsOut || (printing && !!al?.low.some((l) => l.active))

  return (
    <header className="safe-top sticky top-0 z-30 border-b border-border/60 bg-panel/90 backdrop-blur">
      <div className="grid h-13 grid-cols-[auto_minmax(0,1fr)] items-center gap-1.5 overflow-hidden px-3 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] sm:gap-2 md:px-4">
        <div className="hidden min-w-0 items-center gap-2 sm:flex">
          <span className="text-sm font-semibold text-muted tabular-nums">{time}</span>
          {linkBadge}
        </div>
        <div className="flex min-w-0 items-center justify-center gap-2">
        {on && s && (
          <span
            className="shrink-0 rounded-md px-2 py-0.5 text-xs font-extrabold tracking-wide text-black"
            style={{ background: stateColor(s), boxShadow: `0 0 12px -3px ${stateColor(s)}` }}
          >
            {stateText(s)}
          </span>
        )}
        {on && s && printing && (
          <span className="hidden min-w-0 truncate text-sm font-bold tabular-nums sm:inline">
            {Math.floor(s.progress)}%{eta && <span className="ml-2 text-info">→ {eta}</span>}
          </span>
        )}
        <span className="sm:hidden">{linkBadge}</span>
        </div>
        <div className="flex min-w-0 items-center justify-end gap-1.5 sm:gap-2">
        {snap?.busy && <Loader2 size={18} className="animate-spin text-accent" />}
        {on && snap?.obico?.enabled && (
          <IconBtn
            title="Obico AI detection"
            bg={obicoLook(snap).color}
            fg={obicoLook(snap).color === C.card2 ? C.text : '#000'}
            onClick={() => setDialog('obico')}
          >
            <Eye size={16} />
            {obicoLook(snap).alert && '!'}
          </IconBtn>
        )}
        {on && (lowCount > 0 || runsOut) && (
          <IconBtn title="Low filament" bg={lowActive ? C.error : C.warn} fg={lowActive ? '#fff' : '#000'} onClick={() => setDialog('low')}>
            <Droplet size={16} />
            {lowCount || 1}
          </IconBtn>
        )}
        {on && al && al.humid >= 0 && (
          <IconBtn title="AMS humidity" bg={C.info} fg="#fff" onClick={() => setDialog('humid')}>
            <Droplets size={16} />
            {al.humid}%
          </IconBtn>
        )}
        {on && unseen > 0 && (
          <IconBtn title="Notifications" bg={C.info} fg="#fff" onClick={() => setDialog('notif')}>
            <Bell size={16} />
            {unseen}
          </IconBtn>
        )}
        {on && maintCount > 0 && (
          <IconBtn title="Maintenance" bg={maintDue ? C.heat : C.warn} onClick={() => setDialog('maint')}>
            <Wrench size={16} />
            {maintCount}
          </IconBtn>
        )}
        {on && s && s.hms.length > 0 && (
          <IconBtn
            title="Printer messages"
            bg={s.hms_severity <= 1 ? C.error : C.warn}
            fg={s.hms_severity <= 1 ? '#fff' : '#000'}
            onClick={() => setDialog('hms')}
          >
            <TriangleAlert size={16} />
            {s.hms.length}
          </IconBtn>
        )}
        </div>
      </div>
      {snap && dialog === 'hms' && <HmsDialog snap={snap} onClose={() => setDialog(null)} />}
      {snap && dialog === 'maint' && <MaintDialog snap={snap} onClose={() => setDialog(null)} />}
      {snap && dialog === 'notif' && <NotifDialog snap={snap} onClose={() => setDialog(null)} />}
      {snap && dialog === 'obico' && <ObicoDialog snap={snap} onClose={() => setDialog(null)} />}
      {snap && dialog === 'low' && <LowFilamentDialog snap={snap} onClose={() => setDialog(null)} />}
      {snap && dialog === 'humid' && <HumidityDialog snap={snap} onClose={() => setDialog(null)} />}
    </header>
  )
}

function Bordered({ color, children }: { color: string; children: ReactNode }) {
  return (
    <div className="rounded-xl border-l-4 bg-card px-4 py-3" style={{ borderColor: color }}>
      {children}
    </div>
  )
}

export function HmsDialog({ snap, onClose }: { snap: Snapshot; onClose: () => void }) {
  const hms = snap.status?.hms ?? []
  return (
    <Modal
      title="Printer messages"
      onClose={onClose}
      footer={
        <button
          className="btn-danger"
          disabled={!printerReady(snap) || hms.length === 0}
          onClick={async () => {
            if ((await cmd('clear_hms')).ok) onClose()
          }}
        >
          Clear errors
        </button>
      }
    >
      <div className="flex flex-col gap-2">
        {hms.length === 0 && <p className="text-muted">No messages.</p>}
        {hms.map((h, i) => (
          <Bordered key={i} color={h.severity <= 1 ? C.error : h.severity === 2 ? C.warn : C.info}>
            {h.text || 'Unknown message'}
          </Bordered>
        ))}
      </div>
    </Modal>
  )
}

function MaintDialog({ snap, onClose }: { snap: Snapshot; onClose: () => void }) {
  const dlg = useDialogs()
  const items = snap.maintenance?.items ?? []
  return (
    <Modal title="Maintenance" onClose={onClose}>
      <div className="flex flex-col gap-2">
        {items.length === 0 && <p className="text-muted">Nothing due.</p>}
        {items.map((m) => (
          <Bordered key={m.id} color={m.due ? C.error : C.warn}>
            <div className="flex items-center gap-3">
              <div className="min-w-0 flex-1">
                <div className="font-semibold">{m.name}</div>
                <div className="text-sm text-muted">{untilText(m)}</div>
              </div>
              <button
                className="btn"
                onClick={async () => {
                  const ok = await dlg.confirm({
                    title: 'Maintenance done',
                    text: `Mark "${m.name}" as done? This resets its counter.`,
                    ok: 'Done',
                  })
                  if (ok && (await cmd('maintenance_done', { id: m.id })).ok) onClose()
                }}
              >
                ✓ Done
              </button>
            </div>
          </Bordered>
        ))}
      </div>
    </Modal>
  )
}

/** Server times are UTC without a zone ("2026-11-07T22:01:05"): show them in local time. */
function localTime(t: string): string {
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(t) ? t : `${t}Z`)
  if (Number.isNaN(d.getTime())) return t.replace('T', ' ').slice(0, 16)
  return d.toLocaleString('en-GB', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function NotifDialog({ snap, onClose }: { snap: Snapshot; onClose: () => void }) {
  const items = snap.notifications?.items ?? []
  const [seenAtOpen] = useState(snap.notif_seen_id)
  useEffect(() => {
    const newest = snap.notifications?.newest_id ?? -1
    if (newest > snap.notif_seen_id) {
      api('/api/settings', { method: 'PATCH', body: { notif_seen_id: newest } }).catch(() => {})
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  return (
    <Modal title="Notifications" onClose={onClose}>
      <div className="flex flex-col gap-2">
        {items.length === 0 && <p className="text-muted">No notifications in the last 7 days.</p>}
        {items.map((n) => (
          <Bordered key={n.id} color={n.id > seenAtOpen ? C.info : C.card2}>
            <div className="min-w-0 font-semibold [overflow-wrap:anywhere]">{n.title || 'Notification'}</div>
            {n.message && <div className="min-w-0 text-sm whitespace-pre-line text-dim [overflow-wrap:anywhere]">{n.message}</div>}
            <div className="mt-1 text-xs text-muted">{localTime(n.time)}</div>
          </Bordered>
        ))}
      </div>
    </Modal>
  )
}

/** Scores of the running print so far (recorded by the server), refreshed while open. */
function useObicoCurrent(): ObicoRec | null | undefined {
  const [rec, setRec] = useState<ObicoRec | null | undefined>(undefined)
  useEffect(() => {
    let alive = true
    const load = () =>
      api<{ record: ObicoRec | null }>('/api/obico/current')
        .then((d) => alive && setRec(d.record))
        .catch(() => alive && setRec(null))
    load()
    const t = window.setInterval(load, 15000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [])
  return rec
}

function ObicoDialog({ snap, onClose }: { snap: Snapshot; onClose: () => void }) {
  const rec = useObicoCurrent()
  const [lapse, setLapse] = useState(false)
  const o = snap.obico!
  const s = snap.status
  const printing = isPrinting(s)
  let thisPrint = 'Not printing - watches the next print'
  if (printing) {
    if (!o.has_printer || !o.cls) thisPrint = 'Waiting for the first frame'
    else {
      const word = o.cls === 'failure' ? 'FAILURE' : o.cls === 'warning' ? 'Suspicious' : 'Looks good'
      thisPrint = `${word} - score ${o.score.toFixed(2)} / ${o.high.toFixed(2)}, ${o.frames} frames`
    }
  }
  const lightOff = !!s && !s.light
  const rows: [string, ReactNode][] = [
    ['Service', o.error ? <span className="text-error">{o.error}</span> : o.running ? 'Running' : 'Stopped'],
    ['This print', thisPrint],
    [
      'Light',
      lightOff ? (
        <span className={printing ? 'text-warn' : ''}>Off - the camera sees too little</span>
      ) : (
        'On'
      ),
    ],
    ['Settings', `${o.sensitivity || '?'} sensitivity, on failure: ${o.action || '?'}`],
  ]
  return (
    <Modal
      title="Obico AI detection"
      onClose={onClose}
      wide
      footer={
        lightOff ? (
          <button className="btn-warn" disabled={!printerReady(snap)} onClick={() => cmd('light', { on: true })}>
            Light on
          </button>
        ) : undefined
      }
    >
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-3">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-sm font-semibold text-muted">{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      {rec && rec.samples > 0 && (
        <div className="mt-5 flex flex-col gap-2 rounded-xl bg-card p-4">
          <div className="text-xs font-bold tracking-wider text-muted uppercase">This print so far · {rec.job}</div>
          <ObicoBody r={rec} />
        </div>
      )}
      {rec === null && printing && <p className="mt-4 text-sm text-muted">No scores recorded for this print yet.</p>}
      <div className="mt-4 flex items-center gap-3 rounded-xl bg-card px-4 py-3">
        {snap.timelapse_id ? (
          <>
            <TimelapseButton id={snap.timelapse_id} onOpen={() => setLapse(true)} className="btn-accent" />
            <span className="text-sm text-muted">Video of this print so far, with the score of every frame.</span>
          </>
        ) : (
          <span className="text-sm text-muted">
            Timelapse: {snap.settings.record_timelapse ? 'recording starts with the next print' : 'off (Settings > Features)'}. Earlier
            recordings: STATS &gt; Timelapses.
          </span>
        )}
      </div>
      {lapse && <TimelapsePlayer id={snap.timelapse_id} onClose={() => setLapse(false)} />}
    </Modal>
  )
}
