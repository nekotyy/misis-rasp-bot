import { useQuery } from '@tanstack/react-query'
import { Calendar, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { fetchSchedule } from '../api/schedule'
import { DayView } from '../components/DayView'
import { ErrorState } from '../components/ErrorState'
import { usePlatform } from '../hooks/usePlatform'
import { cn } from '../lib/utils'

interface SchedulePageProps {
  subscriptionId?: number
  subscriptionTitle?: string
  reducedMotion?: boolean
}

/** Форматировать дату по-русски */
function formatDate(dateStr: string): { dayNum: string; dayName: string; fullDate: string } {
  const date = new Date(dateStr + 'T00:00:00')
  const dayNum = new Intl.DateTimeFormat('ru', { day: 'numeric', month: 'short' }).format(date)
  const dayName = new Intl.DateTimeFormat('ru', { weekday: 'short' }).format(date)
  const fullDate = new Intl.DateTimeFormat('ru', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  }).format(date)
  return { dayNum, dayName, fullDate }
}

/** Определить «Сегодня» / «Завтра» / «Послезавтра» */
function getDayLabel(dateStr: string): string {
  const today = new Date()
  const d = new Date(dateStr + 'T00:00:00')
  const diff = Math.round(
    (d.getTime() - new Date(today.toISOString().slice(0, 10) + 'T00:00:00').getTime()) / 86400000,
  )
  if (diff === 0) return 'Сегодня'
  if (diff === 1) return 'Завтра'
  if (diff === 2) return 'Послезавтра'
  return new Intl.DateTimeFormat('ru', { weekday: 'short' }).format(d)
}

export function SchedulePage({
  subscriptionId,
  subscriptionTitle,
  reducedMotion = false,
}: SchedulePageProps) {
  const [selectedIndex, setSelectedIndex] = useState(0)
  const platform = usePlatform()

  const {
    data,
    isLoading,
    isRefetching,
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
      <div className="flex-1 flex flex-col justify-center">
        <ErrorState type="no_subscription" />
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex-1 flex flex-col justify-center">
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
  const currentFormatted = selectedDay ? formatDate(selectedDay.date) : null

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Заголовок группы и кнопка обновления */}
      <div className="px-4 pt-1 pb-2 flex items-center justify-between">
        <div>
          <h1 className="font-heading font-extrabold text-xl text-foreground tracking-tight">
            {subscriptionTitle ?? 'Расписание занятий'}
          </h1>
          {currentFormatted && (
            <p className="text-xs text-muted-foreground capitalize mt-0.5 flex items-center gap-1.5 font-medium">
              <Calendar className="w-3.5 h-3.5 text-primary stroke-[2]" />
              <span>{currentFormatted.fullDate}</span>
            </p>
          )}
        </div>

        <button
          onClick={() => {
            platform.hapticFeedback('light')
            refetch()
          }}
          className={cn(
            'p-2 rounded-xl glass-pill hover:bg-white/10 transition-colors text-muted-foreground hover:text-foreground',
            isRefetching && 'animate-spin text-primary',
          )}
          aria-label="Обновить расписание"
        >
          <RefreshCw className="w-4 h-4 stroke-[2]" />
        </button>
      </div>

      {/* Переключатель дней (Стекло + пилюли) */}
      <div className="px-4 pb-3">
        <div className="grid grid-cols-3 gap-2">
          {isLoading
            ? [0, 1, 2].map((i) => (
                <div key={i} className="h-16 rounded-2xl glass-card animate-pulse" />
              ))
            : days.map((day, idx) => {
                const { dayNum, dayName } = formatDate(day.date)
                const isSelected = idx === selectedIndex
                return (
                  <button
                    key={day.date}
                    onClick={() => {
                      platform.hapticFeedback('light')
                      setSelectedIndex(idx)
                    }}
                    className={cn(
                      'flex flex-col items-center justify-center py-2.5 px-1 rounded-2xl transition-all duration-200',
                      isSelected
                        ? 'glass-pill-active scale-[1.02] shadow-lg'
                        : 'glass-card text-muted-foreground hover:text-foreground hover:bg-white/5',
                    )}
                  >
                    <span className="text-[10px] font-semibold uppercase tracking-wider opacity-80 leading-none">
                      {getDayLabel(day.date)}
                    </span>
                    <span className="font-heading font-bold text-sm tracking-tight my-0.5">
                      {dayNum}
                    </span>
                    <span className="text-[10px] font-medium capitalize opacity-70 leading-none">
                      {dayName}
                    </span>
                  </button>
                )
              })}
        </div>
      </div>

      {/* Список занятий выбранного дня */}
      <div className="flex-1 overflow-y-auto px-4 pb-4">
        <DayView day={selectedDay} loading={isLoading} reducedMotion={reducedMotion} />
      </div>
    </div>
  )
}
