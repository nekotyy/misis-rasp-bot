import { apiFetch } from './client'

export type LessonStatus = 'unchanged' | 'changed' | 'added' | 'cancelled'

export interface LessonChangeDto {
  kind: string
  before?: Record<string, unknown>
  after?: Record<string, unknown>
}

export interface LessonDto {
  number: number
  subject: string
  teacher: string
  classroom: string
  time_start?: string
  time_end?: string
  status: LessonStatus
  change?: LessonChangeDto
}

export type DayStatus = 'available' | 'confirmed_empty' | 'unpublished' | 'not_archived'

export interface DayScheduleDto {
  date: string
  day_name: string
  status: DayStatus
  origin: string
  stale: boolean
  observed_at?: string
  lessons: LessonDto[]
}

export interface ScheduleResponse {
  subscription_id: number
  subscription_title: string
  days: DayScheduleDto[]
}

export async function fetchSchedule(subscriptionId: number): Promise<ScheduleResponse> {
  return apiFetch<ScheduleResponse>(`/schedule?subscription_id=${subscriptionId}`)
}
