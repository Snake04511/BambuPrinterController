// Timelapse player: the frames the server recorded during a print, played as a video, with
// the Obico score of every frame below; a click on the chart jumps the video there.
import { ChevronLeft, ChevronRight, Download, Film, Loader2, Pause, Play, Radio } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api, type ApiError } from './api'
import { C } from './format'
import { Modal, useDialogs } from './ui'

/** [epoch s, score | null, class, layer, progress %, light on] */
type Frame = [number, number | null, string, number, number, boolean]

interface Meta {
  id: string
  job: string
  start: number
  end: number
  result: string
  low: number
  high: number
  frames: Frame[]
  active: boolean
  approx?: boolean
}

const SPEEDS = [5, 15, 30]
const frameUrl = (id: string, n: number) => `/api/timelapse/${id}/frame/${n}`
const hhmm = (t: number) =>
  new Date(t * 1000).toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
  })

export function TimelapseButton({ id, onOpen, className = 'btn' }: { id: string; onOpen: () => void; className?: string }) {
  if (!id) return null
  return (
    <button className={className} onClick={onOpen} title="Timelapse with Obico scores">
      <Film size={18} /> Timelapse
    </button>
  )
}

export function TimelapsePlayer({ id, onClose }: { id: string; onClose: () => void }) {
  const [meta, setMeta] = useState<Meta | null>(null)
  const [err, setErr] = useState('')
  const [idx, setIdx] = useState(0)
  const [shown, setShown] = useState(-1) // frame on screen (the next one may still load)
  const [playing, setPlaying] = useState(false)
  const [follow, setFollow] = useState(false) // live: stay on the newest frame
  const [fps, setFps] = useState(15)
  const loaded = useRef<Set<number>>(new Set())

  // Meta; while the print runs, new frames every 20 s.
  useEffect(() => {
    let alive = true
    let first = true
    const load = () =>
      api<Meta>(`/api/timelapse/${id}`)
        .then((m) => {
          if (!alive) return
          setMeta(m)
          if (first) {
            first = false
            const last = Math.max(0, m.frames.length - 1)
            setIdx(m.active ? last : 0) // running print: start at "now"
            setFollow(m.active)
          }
        })
        .catch((e: ApiError) => alive && setErr(e.message))
    load()
    const t = window.setInterval(load, 20000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [id])

  const n = meta?.frames.length ?? 0
  useEffect(() => {
    if (follow && n) setIdx(n - 1)
  }, [follow, n])

  // Playback: advance only when the current frame is on screen (no skipping on a slow link).
  useEffect(() => {
    if (!playing || !n) return
    const t = window.setTimeout(() => {
      if (shown !== idx) return
      if (idx >= n - 1) {
        setPlaying(false)
        if (meta?.active) setFollow(true)
        return
      }
      setIdx(idx + 1)
    }, 1000 / fps)
    return () => window.clearTimeout(t)
  }, [playing, idx, shown, n, fps, meta?.active])

  // Load the wanted frame (and a few ahead) before swapping it in.
  useEffect(() => {
    if (!n) return
    const want = Math.min(idx, n - 1)
    const img = new Image()
    img.onload = () => {
      loaded.current.add(want)
      setShown((s) => (want === idx ? want : s))
    }
    img.src = frameUrl(id, want)
    for (let k = 1; k <= 8 && want + k < n; k++) {
      if (!loaded.current.has(want + k)) {
        const p = new Image()
        p.onload = () => loaded.current.add(want + k)
        p.src = frameUrl(id, want + k)
      }
    }
  }, [id, idx, n])

  const seek = (i: number) => {
    setFollow(false)
    setIdx(Math.max(0, Math.min(n - 1, i)))
  }
  const f = meta && shown >= 0 ? meta.frames[shown] : null

  return (
    <Modal
      title={
        <span className="flex min-w-0 items-center gap-2">
          <Film size={20} className="shrink-0" />
          <span className="truncate">Timelapse · {meta?.job ?? ''}</span>
          {meta?.active && <span className="shrink-0 rounded-md bg-error/20 px-1.5 py-0.5 text-[11px] font-bold text-error">REC</span>}
        </span>
      }
      onClose={onClose}
      wide
    >
      {err && <p className="text-error">{err}</p>}
      {!meta && !err && (
        <p className="flex items-center gap-2 text-muted">
          <Loader2 size={16} className="animate-spin" /> Loading...
        </p>
      )}
      {meta && !n && <p className="text-muted">No frames yet - the first one is taken at the next layer change.</p>}
      {meta && n > 0 && (
        <div className="flex flex-col gap-3">
          <div className="relative aspect-video w-full overflow-hidden rounded-xl bg-black">
            {shown >= 0 && <img src={frameUrl(id, shown)} alt="" className="h-full w-full object-contain" />}
            {f && (
              <div className="absolute bottom-2 left-2 rounded-md bg-black/60 px-2 py-1 text-xs font-semibold text-dim backdrop-blur-sm tabular-nums">
                {hhmm(f[0])} · layer {f[3]} · {Math.floor(f[4])}%{f[1] !== null && ` · score ${f[1].toFixed(2)}`}
                {!f[5] && <span className="text-warn"> · light off</span>}
              </div>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button className="btn h-10 w-10 p-0" onClick={() => seek(idx - 1)} title="Previous frame">
              <ChevronLeft size={18} />
            </button>
            <button
              className="btn-accent h-10 w-14 p-0"
              onClick={() => {
                if (!playing && idx >= n - 1) seek(0)
                setFollow(false)
                setPlaying(!playing)
              }}
              title={playing ? 'Pause' : 'Play'}
            >
              {playing ? <Pause size={20} /> : <Play size={20} />}
            </button>
            <button className="btn h-10 w-10 p-0" onClick={() => seek(idx + 1)} title="Next frame">
              <ChevronRight size={18} />
            </button>
            <input
              type="range"
              min={0}
              max={n - 1}
              value={Math.min(idx, n - 1)}
              onChange={(e) => seek(Number(e.target.value))}
              className="h-2 min-w-32 flex-1 cursor-pointer accent-[var(--color-accent)]"
            />
            <span className="w-24 text-right text-xs text-muted tabular-nums">
              {Math.min(idx, n - 1) + 1} / {n}
            </span>
            <div className="flex gap-0.5 rounded-lg bg-card p-0.5">
              {SPEEDS.map((s) => (
                <button
                  key={s}
                  onClick={() => setFps(s)}
                  className={`rounded-md px-2 py-1 text-xs font-bold ${fps === s ? 'bg-card2 text-accent' : 'text-muted'}`}
                >
                  {s} fps
                </button>
              ))}
            </div>
            <VideoDownload id={id} fps={fps} frames={n} active={meta.active} />
            {meta.active && (
              <button
                className={`btn h-10 text-sm ${follow ? 'text-error' : ''}`}
                onClick={() => {
                  setPlaying(false)
                  setFollow(true)
                }}
                title="Jump to the newest frame and stay there"
              >
                <Radio size={16} /> Live
              </button>
            )}
          </div>

          <ScoreChart meta={meta} idx={Math.min(idx, n - 1)} onSeek={seek} />
          {meta.approx && (
            <p className="text-xs text-muted">
              Imported from the printer (one frame per layer, no times in the file): frame times are spread evenly over the print and the
              Obico scores are matched by that estimate, so they can be off by a while.
            </p>
          )}
        </div>
      )}
    </Modal>
  )
}

/** MP4 of the recording at the chosen speed: made on the server once, then downloaded. */
function VideoDownload({ id, fps, frames, active }: { id: string; fps: number; frames: number; active: boolean }) {
  const dlg = useDialogs()
  const [state, setState] = useState<'idle' | 'making' | 'ready'>('idle')
  const [overlay, setOverlay] = useState(true)
  const url = `/api/timelapse/${id}/video?fps=${fps}&overlay=${overlay}`
  useEffect(() => setState('idle'), [fps, frames, overlay])
  const save = () => {
    const a = document.createElement('a')
    a.href = url
    a.download = ''
    document.body.appendChild(a)
    a.click()
    a.remove()
  }
  const make = async () => {
    setState('making')
    try {
      let st = await api<{ state: string; message: string }>(`/api/timelapse/${id}/video`, { body: { fps, overlay } })
      while (st.state === 'making') {
        await new Promise((r) => window.setTimeout(r, 2000))
        st = await api<{ state: string; message: string }>(`/api/timelapse/${id}/video/status?fps=${fps}&overlay=${overlay}`)
      }
      if (st.state !== 'ready') throw new Error(st.message || 'Video failed')
      setState('ready')
      save() // Safari may block it (no click any more): the button then says Save
    } catch (e) {
      setState('idle')
      dlg.toast(false, (e as ApiError).message ?? String(e))
    }
  }
  const toggle = (
    <label
      className="flex cursor-pointer items-center gap-1.5 text-xs font-semibold text-muted"
      title="Time, layer, % and the Obico score burnt into the video"
    >
      <input
        type="checkbox"
        className="h-4 w-4 accent-[var(--color-accent)]"
        checked={overlay}
        onChange={(e) => setOverlay(e.target.checked)}
      />
      with info
    </label>
  )
  if (state === 'ready')
    return (
      <>
        {toggle}
        <a href={url} download className="btn-accent h-10 text-sm" title="Save the video">
          <Download size={16} /> Save MP4
        </a>
      </>
    )
  return (
    <>
      {toggle}
      <button
        className="btn h-10 text-sm"
        disabled={state === 'making'}
        onClick={make}
        title={`Download as MP4 (${fps} fps, ${Math.round(frames / fps)} s)${active ? ' - frames so far' : ''}`}
      >
        {state === 'making' ? <Loader2 size={16} className="animate-spin" /> : <Download size={16} />}
        {state === 'making' ? 'Making video...' : 'MP4'}
      </button>
    </>
  )
}

/** Obico score per frame; dashed: warning / failure thresholds; grey ticks: light off. */
function ScoreChart({ meta, idx, onSeek }: { meta: Meta; idx: number; onSeek: (i: number) => void }) {
  const fr = meta.frames
  const n = fr.length
  const W = 600
  const H = 110
  const scores = fr.map((x) => x[1]).filter((v): v is number => v !== null)
  if (!scores.length) return <p className="text-sm text-muted">No Obico scores in this recording (Obico off, or no frame analysed yet).</p>
  const top = Math.max(1, ...scores, meta.high || 0) * 1.05
  const x = (i: number) => (n > 1 ? (i / (n - 1)) * W : W / 2)
  const y = (v: number) => H - (v / top) * H
  let path = ''
  let pen = false
  fr.forEach((f, i) => {
    if (f[1] === null) {
      pen = false
      return
    }
    path += `${pen ? 'L' : 'M'}${x(i).toFixed(1)},${y(f[1]).toFixed(1)}`
    pen = true
  })
  const click = (e: React.MouseEvent<SVGSVGElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    onSeek(Math.round(((e.clientX - r.left) / r.width) * (n - 1)))
  }
  // Worst moments: frames over the warning threshold, clickable pins.
  const pins = meta.low > 0 ? fr.map((f, i) => [i, f[1]] as const).filter(([, v]) => v !== null && v >= meta.low) : []
  return (
    <div className="rounded-xl bg-card p-3">
      <div className="mb-1 flex items-center justify-between text-[11px] font-bold tracking-wider text-muted uppercase">
        <span>Obico score · click to jump</span>
        <span className="normal-case tracking-normal">
          {hhmm(fr[0][0])} - {hhmm(fr[n - 1][0])}
        </span>
      </div>
      <svg viewBox={`0 0 ${W} ${H + 8}`} className="w-full cursor-pointer" onClick={click}>
        <rect x="0" y="0" width={W} height={H} fill="transparent" />
        {fr.map((f, i) => (!f[5] ? <rect key={i} x={x(i) - 1} y={H + 3} width="2" height="5" fill={C.muted} opacity="0.6" /> : null))}
        {meta.low > 0 && <line x1="0" x2={W} y1={y(meta.low)} y2={y(meta.low)} stroke={C.warn} strokeDasharray="6 5" strokeWidth="1.2" />}
        {meta.high > 0 && (
          <line x1="0" x2={W} y1={y(meta.high)} y2={y(meta.high)} stroke={C.error} strokeDasharray="6 5" strokeWidth="1.2" />
        )}
        <path
          d={path}
          fill="none"
          stroke={C.info}
          strokeWidth="2"
          strokeLinejoin="round"
          style={{ filter: `drop-shadow(0 0 3px ${C.info})` }}
        />
        {pins.map(([i, v]) => (
          <circle key={i} cx={x(i)} cy={y(v!)} r="4" fill={v! >= meta.high ? C.error : C.warn} stroke="#000" strokeWidth="1" />
        ))}
        <line x1={x(idx)} x2={x(idx)} y1="0" y2={H} stroke={C.accent} strokeWidth="2" />
      </svg>
    </div>
  )
}
