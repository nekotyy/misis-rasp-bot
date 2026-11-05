import { useQuery } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight } from 'lucide-react'
import { useState } from 'react'
import { fetchHistory } from '../api/history'
import type { DayScheduleDto } from '../api/schedule'
import { DayView } from '../components/DayView'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { cn } from '../lib/utils'

interface HistoryPageProps {
  subscriptionId?: number
  reducedMotion?: boolean
}

function formatWeekRange(weekStart: string): string {
  const start = new Date(weekStart + 'T00:00:00')
  const end = new Date(start.getTime() + 6 * 86400000)
  const fmt = new Intl.DateTimeFormat('ru', { day: 'numeric', month: 'short' })
  return `${fmt.format(start)} — ${fmt.format(end)}`
}

function formatDayHeader(dateStr: string, dayName: string): string {
  const date = new Date(dateStr + 'T00:00:00')
  const formatted = new Intl.DateTimeFormat('ru', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  }).format(date)
  return dayName || formatted
}

function AccordionDay({ day, reducedMotion }: { day: DayScheduleDto; reducedMotion: boolean }) {
  const [open, setOpen] = useState(false)

  const hasData =
    day.status === 'available' && day.lessons.length > 0

  return (
    <div className="border border-border rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between p-3 text-left hover:bg-muted/50 transition-colors"
      >
        <div>
          <p className="font-medium text-sm capitalize">{formatDayHeader(day.date, day.day_name)}</p>
          <p className="text-xs text-muted-foreground">
            {day.status === 'unpublished'
              ? 'Не опубликовано'
              : day.status === 'not_archived'
                ? 'Не сохранено'
                : day.status === 'confirmed_empty'
                  ? 'Занятий нет'
                  : `${day.lessons.length} ${plural(day.lessons.length, 'пара', 'пары', 'пар')}`}
          </p>
        </div>
        <ChevronRight
          className={cn(
            'w-4 h-4 text-muted-foreground flex-shrink-0',
            reducedMotion ? '' : 'transition-transform',
            open ? 'rotate-90' : '',
          )}
        />
      </button>
      {open && hasData && (
        <div className="px-3 pb-3 border-t border-border">
          <div className="pt-2">
            <DayView day={day} reducedMotion={reducedMotion} />
          </div>
        </div>
      )}
      {open && !hasData && (
        <div className="px-3 pb-3 border-t border-border">
          <DayView day={day} reducedMotion={reducedMotion} />
        </div>
      )}
    </div>
  )
}

function plural(n: number, one: string, few: string, many: string): string {
  const mod10 = n % 10
  const mod100 = n % 100
  if (mod10 === 1 && mod100 !== 11) return one
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return few
  return many
}

export function HistoryPage({ subscriptionId, reducedMotion = false }: HistoryPageProps) {
  const [weekStart, setWeekStart] = useState<string | undefined>(undefined)

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['history', subscriptionId, weekStart],
    queryFn: () => fetchHistory(subscriptionId!, weekStart),
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

  const days = [...(data?.days ?? [])].sort(
    (a, b) => new Date(b.date).getTime() - new Date(a.date).getTime(),
  )

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Заголовок */}
      <div className="px-4 pt-3 pb-2">
        <h1 className="text-lg font-bold">История</h1>
      </div>

      {/* Навигация по неделям */}
      <div className="px-4 pb-3 flex items-center justify-between gap-2">
        <button
          onClick={() => {
            if (data?.has_previous_week) {
              const start = new Date(data.week_start + 'T00:00:00')
              start.setDate(start.getDate() - 7)
              setWeekStart(start.toISOString().slice(0, 10))
            }
          }}
          disabled={!data?.has_previous_week}
          className="p-2 rounded-lg border border-border disabled:opacity-30 hover:bg-muted transition-colors"
          aria-label="Предыдущая неделя"
        >
          <ChevronLeft className="w-4 h-4" />
        </button>

        <span className="text-sm font-medium flex-1 text-center">
          {isLoading
            ? 'Загрузка...'
            : data
              ? formatWeekRange(data.week_start)
              : 'Текущая неделя'}
        </span>

        <button
          onClick={() => {
            if (data?.has_next_week) {
              const start = new Date(data.week_start + 'T00:00:00')
              start.setDate(start.getDate() + 7)
              setWeekStart(start.toISOString().slice(0, 10))
            }
          }}
          disabled={!data?.has_next_week}
          className="p-2 rounded-lg border border-border disabled:opacity-30 hover:bg-muted transition-colors"
          aria-label="Следующая неделя"
        >
          <ChevronRight className="w-4 h-4" />
        </button>
      </div>

      {/* Дни */}
      <div className="flex-1 overflow-y-auto px-4 pb-2 space-y-2">
        {isLoading ? (
          <LoadingSkeleton count={5} />
        ) : days.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-12 gap-3 text-muted-foreground">
            <span className="text-4xl opacity-30">📚</span>
            <p className="text-sm font-medium">Нет данных за эту неделю</p>
          </div>
        ) : (
          days.map((day) => (
            <AccordionDay key={day.date} day={day} reducedMotion={reducedMotion} />
          ))
        )}
      </div>
    </div>
  )
}
