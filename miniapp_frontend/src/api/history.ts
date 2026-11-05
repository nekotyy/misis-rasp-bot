import { apiFetch } from './client'
import type { DayScheduleDto } from './schedule'

export interface HistoryResponse {
  subscription_id: number
  subscription_title: string
  week_start: string
  days: DayScheduleDto[]
  has_previous_week: boolean
  has_next_week: boolean
  bounds: { start: string; end: string }
}

export async function fetchHistory(
  subscriptionId: number,
  weekStart?: string,
): Promise<HistoryResponse> {
  const params = new URLSearchParams({ subscription_id: String(subscriptionId) })
  if (weekStart) params.set('week_start', weekStart)
  return apiFetch<HistoryResponse>(`/history?${params}`)
}
