import { AlertTriangle, CalendarX, FileQuestion, Sparkles } from 'lucide-react'
import type { DayScheduleDto } from '../api/schedule'
import { LessonCard } from './LessonCard'
import { LoadingSkeleton } from './LoadingSkeleton'

interface DayViewProps {
  day?: DayScheduleDto
  loading?: boolean
  reducedMotion?: boolean
}

export function DayView({ day, loading = false, reducedMotion = false }: DayViewProps) {
  if (loading) {
    return <LoadingSkeleton />
  }

  if (!day) {
    return (
      <div className="glass-card rounded-2xl p-8 flex flex-col items-center justify-center text-center gap-3">
        <div className="w-12 h-12 rounded-2xl bg-neutral-surface border border-border flex items-center justify-center text-neutral">
          <CalendarX className="w-6 h-6 stroke-[1.8]" />
        </div>
        <p className="font-heading font-semibold text-base text-foreground">Нет данных</p>
        <p className="text-xs text-neutral max-w-xs">
          Расписание для выбранного дня не найдено
        </p>
      </div>
    )
  }

  if (day.status === 'unpublished') {
    return (
      <div className="glass-card rounded-2xl p-8 flex flex-col items-center justify-center text-center gap-3">
        <div className="w-12 h-12 rounded-2xl bg-primary-surface border border-primary/30 flex items-center justify-center text-primary">
          <FileQuestion className="w-6 h-6 stroke-[1.8]" />
        </div>
        <p className="font-heading font-semibold text-base text-foreground">
          Расписание не опубликовано
        </p>
        <p className="text-xs text-neutral max-w-xs">
          Учебная часть ещё не разместила расписание на этот день
        </p>
      </div>
    )
  }

  if (day.status === 'confirmed_empty' || day.lessons.length === 0) {
    return (
      <div className="glass-card rounded-2xl p-8 flex flex-col items-center justify-center text-center gap-3">
        <div className="w-12 h-12 rounded-2xl bg-success-surface border border-success/30 flex items-center justify-center text-success">
          <Sparkles className="w-6 h-6 stroke-[1.8]" />
        </div>
        <p className="font-heading font-semibold text-base text-foreground">Занятий нет</p>
        <p className="text-xs text-neutral max-w-xs">
          В этот день пар не запланировано. Отличный повод отдохнуть!
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-3 pb-2">
      {day.stale && (
        <div className="rounded-xl p-3 border border-primary/30 bg-primary-surface text-primary flex items-center gap-2.5 text-xs">
          <AlertTriangle className="w-4 h-4 flex-shrink-0 text-primary" />
          <span>Расписание может быть устаревшим (показана сохранённая копия).</span>
        </div>
      )}
      {day.lessons.map((lesson) => (
        <LessonCard key={lesson.number} lesson={lesson} reducedMotion={reducedMotion} />
      ))}
    </div>
  )
}
