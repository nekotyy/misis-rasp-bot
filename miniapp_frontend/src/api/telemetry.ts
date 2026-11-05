import { apiFetch } from './client'

export interface TelemetrySessionPayload {
  session_id: string
  platform: string
  user_id?: number
  account_id?: string
  user_agent?: string
  screen_width?: number
  screen_height?: number
  theme?: string
  scale?: number
  active_group?: string
  last_page?: string
}

export interface TelemetryPingPayload {
  session_id: string
  duration_seconds: number
  active_group?: string
  current_page?: string
}

export interface TelemetryEventPayload {
  session_id: string
  platform: string
  user_id?: number
  account_id?: string
  event_type: string
  event_data?: Record<string, unknown>
}

export async function sendTelemetrySession(payload: TelemetrySessionPayload): Promise<void> {
  try {
    await apiFetch<void>('/telemetry/session', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  } catch {
    // Телеметрия не должна прерывать работу интерфейса
  }
}

export async function sendTelemetryPing(payload: TelemetryPingPayload): Promise<void> {
  try {
    await apiFetch<void>('/telemetry/ping', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  } catch {
    // Телеметрия не должна прерывать работу интерфейса
  }
}

export async function sendTelemetryEvent(payload: TelemetryEventPayload): Promise<void> {
  try {
    await apiFetch<void>('/telemetry/event', {
      method: 'POST',
      body: JSON.stringify(payload),
    })
  } catch {
    // Телеметрия не должна прерывать работу интерфейса
  }
}
