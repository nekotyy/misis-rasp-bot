import { useQuery } from '@tanstack/react-query'
import { CalendarRange, ChevronLeft, ChevronRight } from 'lucide-react'
import { useState } from 'react'
import { fetchHistory } from '../api/history'
import type { DayScheduleDto } from '../api/schedule'
import { DayView } from '../components/DayView'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { usePlatform } from '../hooks/usePlatform'
import { cn } from '../lib/utils'

interface HistoryPageProps {
  subscriptionId?: number
  reducedMotion?: boolean
}

function formatWeekRange(weekStart: string): string {
  const start = new Date(weekStart + 'T00:00:00')
  const end = new Date(start.getTime() + 6 * 86400000)
  const fmt = new Intl.DateTimeFormat('ru', { day: 'numeric', month: 'short' })
  return `${fmt.format(start)} – ${fmt.format(end)}`
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
  const platform = usePlatform()

  const hasData = day.status === 'available' && day.lessons.length > 0

  return (
    <div className="glass-card rounded-2xl overflow-hidden transition-all duration-200">
      <button
        onClick={() => {
          platform.hapticFeedback('light')
          setOpen(!open)
        }}
        className="w-full flex items-center justify-between p-3.5 text-left hover:bg-white/5 transition-colors"
      >
        <div className="min-w-0 pr-2">
          <p className="font-heading font-bold text-sm text-foreground capitalize tracking-tight truncate">
            {formatDayHeader(day.date, day.day_name)}
          </p>
          <p className="text-xs text-neutral mt-0.5">
            {day.status === 'unpublished'
              ? 'Не опубликовано'
              : day.status === 'not_archived'
                ? 'Не сохранено'
                : day.status === 'confirmed_empty'
                  ? 'Занятий нет'
                  : `${day.lessons.length} ${plural(day.lessons.length, 'пара', 'пары', 'пар')}`}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          {hasData && (
            <span className="font-heading glass-pill rounded-full px-2 py-0.5 text-[10px] font-bold text-primary bg-primary-surface border border-primary/20">
              {day.lessons.length}
            </span>
          )}
          <ChevronRight
            className={cn(
              'w-4 h-4 text-neutral transition-transform duration-200',
              open ? 'rotate-90 text-primary' : '',
              reducedMotion && 'transition-none',
            )}
          />
        </div>
      </button>
      {open && (
        <div className="px-3.5 pb-3.5 pt-1 border-t border-border/40">
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
  const platform = usePlatform()

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['history', subscriptionId, weekStart],
    queryFn: () => fetchHistory(subscriptionId!, weekStart),
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

  const days = [...(data?.days ?? [])].sort(
    (a, b) => new Date(b.date).getTime() - new Date(a.date).getTime(),
  )

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Заголовок */}
      <div className="px-4 pt-1 pb-2">
        <h1 className="font-heading font-extrabold text-xl text-foreground tracking-tight">
          Архив расписания
        </h1>
        <p className="text-xs text-neutral mt-0.5 font-medium">
          История проведённых пар за последние 3 месяца
        </p>
      </div>

      {/* Навигация по неделям */}
      <div className="px-4 pb-3 flex items-center justify-between gap-2">
        <button
          onClick={() => {
            if (data?.has_previous_week) {
              platform.hapticFeedback('light')
              const start = new Date(data.week_start + 'T00:00:00')
              start.setDate(start.getDate() - 7)
              setWeekStart(start.toISOString().slice(0, 10))
            }
          }}
          disabled={!data?.has_previous_week}
          className="p-2.5 rounded-xl glass-card disabled:opacity-30 hover:bg-neutral-surface transition-colors text-neutral hover:text-foreground active:scale-95 cursor-pointer"
          aria-label="Предыдущая неделя"
        >
          <ChevronLeft className="w-4 h-4 stroke-[2]" />
        </button>

        <div className="glass-pill rounded-xl px-4 py-2 flex-1 text-center font-heading font-semibold text-xs tracking-tight text-foreground shadow-sm bg-neutral-surface border border-border">
          {isLoading
            ? 'Загрузка недели...'
            : data
              ? formatWeekRange(data.week_start)
              : 'Текущая неделя'}
        </div>

        <button
          onClick={() => {
            if (data?.has_next_week) {
              platform.hapticFeedback('light')
              const start = new Date(data.week_start + 'T00:00:00')
              start.setDate(start.getDate() + 7)
              setWeekStart(start.toISOString().slice(0, 10))
            }
          }}
          disabled={!data?.has_next_week}
          className="p-2.5 rounded-xl glass-card disabled:opacity-30 hover:bg-neutral-surface transition-colors text-neutral hover:text-foreground active:scale-95 cursor-pointer"
          aria-label="Следующая неделя"
        >
          <ChevronRight className="w-4 h-4 stroke-[2]" />
        </button>
      </div>

      {/* Список дней недели */}
      <div className="flex-1 overflow-y-auto px-4 pb-4 space-y-2.5">
        {isLoading ? (
          <LoadingSkeleton count={5} />
        ) : days.length === 0 ? (
          <div className="glass-card rounded-2xl p-8 flex flex-col items-center justify-center text-center gap-3">
            <div className="w-12 h-12 rounded-2xl bg-neutral-surface border border-border flex items-center justify-center text-neutral">
              <CalendarRange className="w-6 h-6 stroke-[1.8]" />
            </div>
            <p className="font-heading font-semibold text-base text-foreground">
              Нет данных за эту неделю
            </p>
            <p className="text-xs text-neutral max-w-xs">
              Записи архива для выбранной недели отсутствуют
            </p>
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
