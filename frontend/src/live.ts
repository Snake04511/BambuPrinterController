// Live snapshot over the backend WebSocket (/api/ws), with reconnect.
import { useSyncExternalStore } from 'react'
import type { Snapshot } from './types'

type Listener = () => void

let snapshot: Snapshot | null = null
let connected = false
const listeners = new Set<Listener>()
let ws: WebSocket | null = null
let retry = 0
let timer: number | undefined
let stopped = true
let onAuthLost: (() => void) | null = null

function emit() {
  for (const l of listeners) l()
}

function connect() {
  if (stopped) return
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  ws = new WebSocket(`${proto}://${location.host}/api/ws`)
  ws.onopen = () => {
    retry = 0
    connected = true
    emit()
  }
  ws.onmessage = (ev) => {
    try {
      snapshot = JSON.parse(ev.data) as Snapshot
      emit()
    } catch {
      /* ignore */
    }
  }
  ws.onclose = (ev) => {
    ws = null
    connected = false
    emit()
    if (ev.code === 4401) {
      onAuthLost?.()
      return
    }
    if (stopped) return
    const delay = Math.min(1000 * 2 ** retry, 10000)
    retry++
    timer = window.setTimeout(connect, delay)
  }
}

export function startLive(authLost: () => void) {
  onAuthLost = authLost
  if (!stopped) return
  stopped = false
  connect()
  // iOS suspends sockets in the background: reconnect right away when visible again.
  document.addEventListener('visibilitychange', onVisible)
}

function onVisible() {
  if (document.visibilityState === 'visible' && !ws && !stopped) {
    window.clearTimeout(timer)
    retry = 0
    connect()
  }
}

export function stopLive() {
  stopped = true
  window.clearTimeout(timer)
  document.removeEventListener('visibilitychange', onVisible)
  ws?.close()
  ws = null
  snapshot = null
}

function subscribe(l: Listener) {
  listeners.add(l)
  return () => listeners.delete(l)
}

export function useSnapshot(): Snapshot | null {
  return useSyncExternalStore(subscribe, () => snapshot)
}

export function useSocketConnected(): boolean {
  return useSyncExternalStore(subscribe, () => connected)
}
