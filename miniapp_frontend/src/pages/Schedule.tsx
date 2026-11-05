import { useQuery } from '@tanstack/react-query'
import { Calendar, ChevronDown, Layers, RefreshCw } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import type { SubscriptionDto } from '../api/profile'
import { fetchSchedule } from '../api/schedule'
import { DayView } from '../components/DayView'
import { ErrorState } from '../components/ErrorState'
import { usePlatform } from '../hooks/usePlatform'
import { cn } from '../lib/utils'

interface SchedulePageProps {
  subscriptionId?: number
  subscriptionTitle?: string
  subscriptions?: SubscriptionDto[]
  onSelectSubscription?: (id: number) => void
  reducedMotion?: boolean
}

/** Форматировать дату по-русски */
function formatDate(dateStr: string): { dayNum: string; dayName: string; fullDate: string } {
  const [y, m, d] = dateStr.split('-').map(Number)
  const date = new Date(y, m - 1, d)
  const dayNum = new Intl.DateTimeFormat('ru', { day: 'numeric', month: 'short' }).format(date)
  const dayName = new Intl.DateTimeFormat('ru', { weekday: 'short' }).format(date)
  const fullDate = new Intl.DateTimeFormat('ru', {
    weekday: 'long',
    day: 'numeric',
    month: 'long',
  }).format(date)
  return {
    dayNum,
    dayName: dayName.charAt(0).toUpperCase() + dayName.slice(1),
    fullDate: fullDate.charAt(0).toUpperCase() + fullDate.slice(1),
  }
}

/** Определить подпись для 3-х дневного переключателя (Сегодня / Завтра / Послезавтра) */
function getDayLabel(dateStr: string, index: number): string {
  if (index === 0) return 'Сегодня'
  if (index === 1) return 'Завтра'
  if (index === 2) return 'Послезавтра'
  const [y, m, d] = dateStr.split('-').map(Number)
  return new Intl.DateTimeFormat('ru', { weekday: 'short' }).format(new Date(y, m - 1, d))
}

export function SchedulePage({
  subscriptionId,
  subscriptionTitle,
  subscriptions = [],
  onSelectSubscription,
  reducedMotion = false,
}: SchedulePageProps) {
  const [selectedIndex, setSelectedIndex] = useState(0)
  const [showGroupPicker, setShowGroupPicker] = useState(false)
  const navigate = useNavigate()
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
        <ErrorState
          type="no_subscription"
          actionLabel="Выбрать группу"
          onAction={() => navigate('/profile')}
        />
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
      <div className="px-4 pt-2 pb-2.5 flex items-center justify-between">
        <div>
          {subscriptions.length > 1 ? (
            <button
              type="button"
              onClick={() => {
                platform.hapticFeedback('light')
                setShowGroupPicker(true)
              }}
              className="inline-flex items-center gap-2 group cursor-pointer text-left"
            >
              <h1 className="font-heading font-extrabold text-2xl text-foreground tracking-tight group-hover:text-primary transition-colors">
                {subscriptionTitle ?? 'Расписание'}
              </h1>
              <span className="p-1 rounded-lg bg-neutral-surface text-neutral group-hover:text-primary group-hover:bg-primary-surface transition-colors">
                <ChevronDown className="w-4 h-4" />
              </span>
            </button>
          ) : (
            <h1 className="font-heading font-extrabold text-2xl text-foreground tracking-tight">
              {subscriptionTitle ?? 'Расписание занятий'}
            </h1>
          )}
          {currentFormatted && (
            <p className="text-sm capitalize mt-1 flex items-center gap-2 font-medium text-gray-700 [data-theme=dark]:text-white dark:text-white">
              <Calendar className="w-4 h-4 text-primary stroke-[2]" />
              <span className="text-gray-700 [data-theme=dark]:text-white dark:text-white">
                {currentFormatted.fullDate}
              </span>
            </p>
          )}
        </div>

        <button
          onClick={() => {
            platform.hapticFeedback('light')
            refetch()
          }}
          className={cn(
            'p-2.5 rounded-xl glass-pill hover:bg-neutral-surface transition-colors text-neutral hover:text-foreground cursor-pointer',
            isRefetching && 'animate-spin text-primary',
          )}
          aria-label="Обновить расписание"
        >
          <RefreshCw className="w-4.5 h-4.5 stroke-[2]" />
        </button>
      </div>

      {/* Модальное окно выбора группы — поднято над нижней панелью навигации */}
      {showGroupPicker && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4 pb-28 sm:pb-4 animate-in fade-in duration-150"
          onClick={() => setShowGroupPicker(false)}
        >
          <div
            className="w-full max-w-sm glass-card rounded-2xl p-4 space-y-3 shadow-2xl border border-primary/30 my-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between">
              <span className="font-heading text-sm font-bold text-foreground flex items-center gap-2">
                <Layers className="w-4 h-4 text-primary" />
                Выберите группу
              </span>
              <button
                type="button"
                onClick={() => setShowGroupPicker(false)}
                className="text-xs text-neutral hover:text-foreground cursor-pointer font-bold"
              >
                Закрыть
              </button>
            </div>
            <div className="space-y-2 max-h-60 overflow-y-auto">
              {subscriptions.map((sub) => {
                const isSelected = sub.id === subscriptionId
                return (
                  <button
                    key={sub.id}
                    type="button"
                    onClick={() => {
                      platform.hapticFeedback('light')
                      onSelectSubscription?.(sub.id)
                      setShowGroupPicker(false)
                    }}
                    className={cn(
                      'w-full flex items-center justify-between p-3 rounded-xl border text-left transition-all cursor-pointer',
                      isSelected
                        ? 'bg-primary text-white border-primary shadow-sm font-bold'
                        : 'bg-card border-border text-foreground hover:bg-neutral-surface',
                    )}
                  >
                    <span className="font-heading text-base font-bold">
                      {sub.group_name || sub.subscription_title || 'Группа'}
                    </span>
                    {sub.is_primary && (
                      <span
                        className={cn(
                          'text-xs px-2 py-0.5 rounded-full font-semibold',
                          isSelected
                            ? 'bg-white/20 text-white'
                            : 'bg-primary-surface text-primary border border-primary/20',
                        )}
                      >
                        Основная
                      </span>
                    )}
                  </button>
                )
              })}
            </div>
          </div>
        </div>
      )}

      {/* Переключатель на 3 дня (Сегодня / Завтра / Послезавтра) */}
      <div className="px-4 pb-3.5">
        <div className="grid grid-cols-3 gap-2.5">
          {isLoading
            ? [0, 1, 2].map((i) => (
                <div key={i} className="h-18 rounded-2xl glass-card animate-pulse" />
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
                      'flex flex-col items-center justify-center py-3 px-1.5 rounded-2xl transition-all duration-200 cursor-pointer border',
                      isSelected
                        ? 'bg-primary text-white border-primary shadow-lg shadow-primary/30 scale-[1.02] font-bold'
                        : 'glass-card border-border hover:bg-neutral-hover/10 text-gray-800 [data-theme=dark]:text-white dark:text-white',
                    )}
                  >
                    <span
                      className={cn(
                        'font-heading text-xs font-bold uppercase tracking-wider leading-none',
                        isSelected ? 'text-white' : 'text-gray-500 [data-theme=dark]:text-white/70 dark:text-white/70',
                      )}
                    >
                      {getDayLabel(day.date, idx)}
                    </span>
                    <span
                      className={cn(
                        'font-heading font-extrabold text-base tracking-tight my-1',
                        isSelected ? 'text-white' : 'text-gray-900 [data-theme=dark]:text-white dark:text-white',
                      )}
                    >
                      {dayNum}
                    </span>
                    <span
                      className={cn(
                        'font-heading text-xs font-semibold capitalize opacity-80 leading-none',
                        isSelected ? 'text-white' : 'text-gray-500 [data-theme=dark]:text-white/70 dark:text-white/70',
                      )}
                    >
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
