import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { fetchSchedule } from '../api/schedule'
import { DayView } from '../components/DayView'
import { ErrorState } from '../components/ErrorState'
import { cn } from '../lib/utils'

interface SchedulePageProps {
  subscriptionId?: number
  subscriptionTitle?: string
  reducedMotion?: boolean
}

/** Форматировать дату по-русски */
function formatDate(dateStr: string): { dayNum: string; dayName: string } {
  const date = new Date(dateStr + 'T00:00:00')
  const dayNum = new Intl.DateTimeFormat('ru', { day: 'numeric', month: 'long' }).format(date)
  const dayName = new Intl.DateTimeFormat('ru', { weekday: 'short' }).format(date)
  return { dayNum, dayName }
}

/** Определить «сегодня» / «завтра» / «послезавтра» */
function getDayLabel(dateStr: string): string {
  const today = new Date()
  const d = new Date(dateStr + 'T00:00:00')
  const diff =
    Math.round((d.getTime() - new Date(today.toISOString().slice(0, 10) + 'T00:00:00').getTime()) /
      86400000)
  if (diff === 0) return 'Сегодня'
  if (diff === 1) return 'Завтра'
  if (diff === 2) return 'Послезавтра'
  return new Intl.DateTimeFormat('ru', { weekday: 'long' }).format(d)
}

export function SchedulePage({
  subscriptionId,
  subscriptionTitle,
  reducedMotion = false,
}: SchedulePageProps) {
  const [selectedIndex, setSelectedIndex] = useState(0)

  const {
    data,
    isLoading,
    error,
    refetch,
  } = useQuery({
    queryKey: ['schedule', subscriptionId],
    queryFn: () => fetchSchedule(subscriptionId!),
    enabled: !!subscriptionId,
    staleTime: 5 * 60 * 1000,
  })

  if (!subscriptionId) {
    return (
      <div className="flex-1 flex flex-col">
        <ErrorState type="no_subscription" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex-1 flex flex-col">
        <ErrorState
          type="server"
          message={(error as Error).message}
          onRetry={() => refetch()}
        />
      </div>
    )
  }

  const days = data?.days ?? []
  const selectedDay = days[selectedIndex]

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Заголовок */}
      <div className="px-4 pt-3 pb-2">
        <h1 className="text-lg font-bold">{subscriptionTitle ?? 'Расписание'}</h1>
      </div>

      {/* Переключатель дней */}
      <div className="px-4 pb-2 flex gap-2">
        {isLoading
          ? [0, 1, 2].map((i) => (
              <div key={i} className="flex-1 h-14 rounded-xl bg-muted animate-pulse" />
            ))
          : days.map((day, idx) => {
              const { dayNum, dayName } = formatDate(day.date)
              return (
                <button
                  key={day.date}
                  onClick={() => setSelectedIndex(idx)}
                  className={cn(
                    'flex-1 flex flex-col items-center justify-center py-2 rounded-xl border text-xs font-medium',
                    reducedMotion ? '' : 'transition-colors',
                    idx === selectedIndex
                      ? 'bg-primary text-primary-foreground border-primary'
                      : 'bg-card border-border text-muted-foreground hover:text-foreground',
                  )}
                >
                  <span className="text-[10px] opacity-70">{getDayLabel(day.date)}</span>
                  <span className="font-bold text-sm">{dayNum}</span>
                  <span className="text-[10px] opacity-70">{dayName}</span>
                </button>
              )
            })}
      </div>

      {/* Список пар */}
      <div className="flex-1 overflow-y-auto px-4 pb-2">
        <DayView day={selectedDay} loading={isLoading} reducedMotion={reducedMotion} />
      </div>
    </div>
  )
}
