// 3D view of the running plate: the outer walls from its G-code (served by the server).
// Progress mode: printed layers solid, the current one glowing, the rest a faint ghost.
// Objects mode (Skip objects): every object in its own colour, tap one to pick it.
// Drag sideways to turn, the slider on the right scrubs through the layers.
import { Box, Loader2, Radio } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { Snapshot } from './types'

interface Layer {
  n: number
  z: number
  p: number[][] // [tool, object id, x0, y0, x1, y1, ...] in 0.1 mm
}
export interface Paths {
  v?: number
  bbox: [number, number, number, number]
  zmax: number
  layers: Layer[]
  layers_total?: number
}

export interface Obj3D {
  id: number
  color: string
  skipped: boolean
  selected: boolean
}

const TILT = 0.62 // ~35 degrees looking down
// distinct, readable on the dark background
export const OBJ_COLORS = ['#38FF9C', '#3BA7FF', '#FFB020', '#C77DFF', '#FF6FB5', '#3DE2E2', '#B8F35A', '#FF8A3D']

/** The running plate's paths (the server may need a moment to fetch / read the file). */
export function usePrintPaths(job: string): {
  data: Paths | null
  msg: string
} {
  const [data, setData] = useState<Paths | null>(null)
  const [msg, setMsg] = useState('Loading the print...')
  useEffect(() => {
    let alive = true
    let timer: number | undefined
    setData(null)
    setMsg('Loading the print...')
    if (!job) return
    const load = () =>
      // job + format in the URL: the browser caches it, but never another print's or an older format
      fetch(`/api/print/paths?f=3&job=${encodeURIComponent(job)}`, { credentials: 'same-origin' })
        .then(async (r) => {
          if (!alive) return
          if (r.status === 202) {
            setMsg('Reading the file from the printer...')
            timer = window.setTimeout(load, 4000)
          } else if (r.ok) setData(await r.json())
          else setMsg((await r.json().catch(() => null))?.detail || 'No 3D view for this print')
        })
        .catch(() => alive && setMsg('Could not load the print'))
    load()
    return () => {
      alive = false
      window.clearTimeout(timer)
    }
  }, [job])
  return { data, msg }
}

export function Progress3D({ snap }: { snap: Snapshot }) {
  const st = snap.status
  const { data, msg } = usePrintPaths(st?.job ?? '')
  return (
    <Paths3D data={data} msg={msg} live={st?.layer && st.layer > 0 ? st.layer : 0} job={st?.job ?? ''} totalHint={st?.total_layers ?? 0} />
  )
}

export function Paths3D({
  data,
  msg,
  live,
  job,
  totalHint = 0,
  objects,
  onPick,
}: {
  data: Paths | null
  msg: string
  live: number
  job: string
  totalHint?: number
  objects?: Obj3D[]
  onPick?: (id: number) => void
}) {
  const [angle, setAngle] = useState(-0.6)
  const canvas = useRef<HTMLCanvasElement>(null)
  const box = useRef<HTMLDivElement>(null)
  const drag = useRef<{
    x: number
    y: number
    a: number
    moved: boolean
  } | null>(null)
  const hits = useRef<{ x: number; y: number; id: number }[]>([])
  const [size, setSize] = useState({ w: 0, h: 0 })
  const [labels, setLabels] = useState<{ id: number; x: number; y: number }[]>([])

  useEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // scrub through the print with the slider; null = follow the printer
  const [pick, setPick] = useState<number | null>(null)
  useEffect(() => setPick(null), [job])
  const total = data?.layers_total ?? totalHint
  const layerNow = pick ?? live
  const v3 = (data?.v ?? 1) >= 3
  const start = v3 ? 2 : 1 // first coordinate in a path
  const objKey = objects?.map((o) => `${o.id}${o.skipped ? 's' : ''}${o.selected ? 'x' : ''}`).join(',') ?? ''

  useEffect(() => {
    const c = canvas.current
    if (!c || !data || !size.w || !size.h) return
    const dpr = window.devicePixelRatio || 1
    c.width = size.w * dpr
    c.height = size.h * dpr
    const g = c.getContext('2d')!
    g.setTransform(dpr, 0, 0, dpr, 0, 0)
    g.clearRect(0, 0, size.w, size.h)
    g.lineJoin = 'round'
    const [x0, y0, x1, y1] = data.bbox
    const cx = (x0 + x1) / 2
    const cy = (y0 + y1) / 2
    const ca = Math.cos(angle)
    const sa = Math.sin(angle)
    const sT = Math.sin(TILT)
    const cT = Math.cos(TILT)
    let minU = Infinity
    let maxU = -Infinity
    let minV = Infinity
    let maxV = -Infinity
    for (const [x, y] of [
      [x0, y0],
      [x1, y0],
      [x0, y1],
      [x1, y1],
    ])
      for (const z of [0, data.zmax]) {
        const u = (x - cx) * ca - (y - cy) * sa
        const v = z * cT + ((x - cx) * sa + (y - cy) * ca) * sT
        minU = Math.min(minU, u)
        maxU = Math.max(maxU, u)
        minV = Math.min(minV, v)
        maxV = Math.max(maxV, v)
      }
    const pad = 18
    const scale = Math.min((size.w - 2 * pad - 30) / Math.max(1, maxU - minU), (size.h - 2 * pad) / Math.max(1, maxV - minV))
    const ox = (size.w - 30) / 2 - ((minU + maxU) / 2) * scale
    const oy = size.h / 2 + ((minV + maxV) / 2) * scale
    const objs = new Map((objects ?? []).map((o) => [o.id, o]))
    const hitList: { x: number; y: number; id: number }[] = []
    // style: (layer, object id) -> stroke or null (= skip the path)
    const draw = (l: Layer, style: (obj: number) => string | null, width: number, collect = false) => {
      const zc = l.z * cT
      let cur = ''
      g.lineWidth = width
      for (const p of l.p) {
        const id = v3 ? p[1] : -1
        const s = style(id)
        if (!s) continue
        if (s !== cur) {
          if (cur) g.stroke()
          g.strokeStyle = s
          g.beginPath()
          cur = s
        }
        for (let k = start; k < p.length; k += 2) {
          const x = p[k] / 10 - cx
          const y = p[k + 1] / 10 - cy
          const sx = ox + (x * ca - y * sa) * scale
          const sy = oy - (zc + (x * sa + y * ca) * sT) * scale
          if (k === start) g.moveTo(sx, sy)
          else g.lineTo(sx, sy)
          if (collect && id >= 0 && (k - start) % 8 === 0) hitList.push({ x: sx, y: sy, id })
        }
      }
      if (cur) g.stroke()
    }
    const zmax = Math.max(0.1, data.zmax)
    const objColor = (id: number, alpha: number) => {
      const o = objs.get(id)
      if (!o) return `rgba(200,200,200,${alpha})`
      if (o.selected) return `rgba(255,77,94,${Math.min(1, alpha + 0.25)})`
      if (o.skipped) return `rgba(90,90,100,${alpha * 0.6})`
      const h = o.color.replace('#', '')
      return `rgba(${parseInt(h.slice(0, 2), 16)},${parseInt(h.slice(2, 4), 16)},${parseInt(h.slice(4, 6), 16)},${alpha})`
    }
    if (objects) {
      // objects mode: the whole model by object, what is printed already brighter
      data.layers.forEach((l, i) => {
        const done = l.n < layerNow
        if (!done && i % 2) return
        draw(l, (id) => objColor(id, done ? 0.85 : 0.22), done ? 1.2 : 1, i % 3 === 0)
      })
    } else {
      data.layers.forEach((l, i) => {
        if (l.n > layerNow && i % 2 === 0) draw(l, () => 'rgba(255,255,255,0.05)', 1)
      })
      for (const l of data.layers) {
        if (l.n >= layerNow) break
        const t = l.z / zmax
        const s = `rgba(${Math.round(30 + 26 * t)}, ${Math.round(150 + 105 * t)}, ${Math.round(95 + 61 * t)}, 0.75)`
        draw(l, () => s, 1)
      }
    }
    const cur = data.layers.find((l) => l.n === layerNow) ?? data.layers.filter((l) => l.n <= layerNow).pop()
    if (cur) {
      g.shadowColor = '#38FF9C'
      g.shadowBlur = 8
      draw(cur, (id) => (objects && objs.get(id)?.selected ? '#FF4D5E' : '#ffffff'), 1.8)
      g.shadowBlur = 0
    }
    hits.current = hitList
    if (objects) {
      // a number label at the middle of each object's drawn points
      const acc = new Map<number, { x: number; y: number; n: number }>()
      for (const h of hitList) {
        const a = acc.get(h.id) ?? { x: 0, y: 0, n: 0 }
        a.x += h.x
        a.y += h.y
        a.n++
        acc.set(h.id, a)
      }
      const next = [...acc].map(([id, a]) => ({ id, x: Math.round(a.x / a.n), y: Math.round(a.y / a.n) }))
      setLabels((old) => (JSON.stringify(old) === JSON.stringify(next) ? old : next))
    }
  }, [data, size, angle, layerNow, objKey])

  const pickAt = (clientX: number, clientY: number) => {
    if (!onPick || !box.current) return
    const r = box.current.getBoundingClientRect()
    const x = clientX - r.left
    const y = clientY - r.top
    let best = -1
    let bd = 26 * 26 // touch-friendly radius
    for (const h of hits.current) {
      const d = (h.x - x) ** 2 + (h.y - y) ** 2
      if (d < bd) {
        bd = d
        best = h.id
      }
    }
    if (best >= 0) onPick(best)
  }

  const z = data?.layers.find((l) => l.n === layerNow)?.z
  return (
    <div
      ref={box}
      className="relative h-full w-full touch-pan-y select-none"
      style={{
        background: 'radial-gradient(ellipse at 50% 70%, #16202a 0%, #0b0e13 75%)',
        cursor: data ? (onPick ? 'pointer' : 'grab') : 'default',
      }}
      onPointerDown={(e) => {
        drag.current = { x: e.clientX, y: e.clientY, a: angle, moved: false }
        ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
      }}
      onPointerMove={(e) => {
        const d = drag.current
        if (!d) return
        if (Math.abs(e.clientX - d.x) > 6) d.moved = true
        if (d.moved) setAngle(d.a + (e.clientX - d.x) / 120)
      }}
      onPointerUp={(e) => {
        const d = drag.current
        drag.current = null
        if (d && !d.moved) pickAt(e.clientX, e.clientY) // a tap, not a turn
      }}
      onPointerCancel={() => (drag.current = null)}
    >
      <canvas ref={canvas} className="absolute inset-0 h-full w-full" />
      {objects &&
        labels.map((l) => {
          const i = objects.findIndex((o) => o.id === l.id)
          const o = objects[i]
          if (!o) return null
          return (
            <span
              key={l.id}
              className="pointer-events-none absolute flex h-6 min-w-6 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border-2 px-1 text-xs font-bold"
              style={{
                left: l.x,
                top: l.y,
                background: '#0b0e13cc',
                borderColor: o.skipped ? '#555' : o.selected ? '#FF4D5E' : o.color,
                color: o.skipped ? '#888' : '#fff',
              }}
            >
              {o.skipped ? '✕' : i + 1}
            </span>
          )
        })}
      {!data && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-4 text-center text-sm text-muted">
          {msg.startsWith('Loading') || msg.startsWith('Reading') ? <Loader2 className="animate-spin" /> : <Box />}
          {msg}
        </div>
      )}
      {data && (
        <div className="absolute bottom-2 left-2 flex items-center gap-2">
          <div className="rounded-md bg-black/60 px-2 py-1 text-xs font-semibold text-dim tabular-nums backdrop-blur-sm">
            {pick !== null && <span className="text-info">view · </span>}
            layer {layerNow || '-'}/{total || '?'}
            {z !== undefined && ` · ${z.toFixed(2)} mm`}
          </div>
          {pick !== null && (
            <button
              className="flex items-center gap-1 rounded-md bg-accent px-2 py-1 text-xs font-bold text-black"
              onPointerDown={(e) => e.stopPropagation()}
              onPointerUp={(e) => e.stopPropagation()}
              onClick={() => setPick(null)}
              title="Back to the layer printing now"
            >
              <Radio size={12} /> Now
            </button>
          )}
        </div>
      )}
      {data && total > 1 && (
        <div
          className="absolute top-3 right-2 bottom-3 flex w-8 flex-col items-center gap-1"
          onPointerDown={(e) => e.stopPropagation()}
          onPointerUp={(e) => e.stopPropagation()}
        >
          <span className="text-[10px] font-bold text-muted">top</span>
          <input
            type="range"
            min={1}
            max={total}
            value={layerNow || 1}
            onChange={(e) => {
              const v = Number(e.target.value)
              setPick(v === live ? null : v)
            }}
            className="min-h-0 w-6 flex-1 cursor-pointer accent-[var(--color-accent)] [direction:rtl] [writing-mode:vertical-lr]"
            title="Drag to see the print at any layer"
          />
          <span className="text-[10px] font-bold text-muted">1</span>
        </div>
      )}
    </div>
  )
}
