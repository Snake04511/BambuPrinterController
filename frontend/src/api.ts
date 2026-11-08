// Small fetch helpers. Every call carries the session cookie (same origin).

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn
}

export async function api<T = unknown>(path: string, init?: { method?: string; body?: unknown }): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(path, {
      method: init?.method ?? (init?.body !== undefined ? 'POST' : 'GET'),
      headers: init?.body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: init?.body !== undefined ? JSON.stringify(init.body) : undefined,
      credentials: 'same-origin',
    })
  } catch {
    throw new ApiError(0, 'Server not reachable')
  }
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`
    try {
      const d = await resp.json()
      if (typeof d?.detail === 'string') msg = d.detail
    } catch {
      /* not JSON */
    }
    if (resp.status === 401 && !path.startsWith('/api/auth/')) onUnauthorized?.()
    throw new ApiError(resp.status, msg)
  }
  return (await resp.json()) as T
}

export interface CmdResult {
  ok: boolean
  message: string
}

/** Printer / app command. The backend also posts the outcome as a notice (toast). */
export function cmd(name: string, args: Record<string, unknown> = {}): Promise<CmdResult> {
  return api<CmdResult>(`/api/cmd/${name}`, { body: args }).catch((e: ApiError) => ({ ok: false, message: e.message }))
}
