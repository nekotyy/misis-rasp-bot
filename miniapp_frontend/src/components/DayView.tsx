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
        <div className="w-12 h-12 rounded-2xl bg-muted/40 border border-border/50 flex items-center justify-center text-muted-foreground">
          <CalendarX className="w-6 h-6 stroke-[1.8]" />
        </div>
        <p className="font-heading font-semibold text-base text-foreground">Нет данных</p>
        <p className="text-xs text-muted-foreground max-w-xs">
          Расписание для выбранного дня не найдено
        </p>
      </div>
    )
  }

  if (day.status === 'unpublished') {
    return (
      <div className="glass-card rounded-2xl p-8 flex flex-col items-center justify-center text-center gap-3">
        <div className="w-12 h-12 rounded-2xl bg-primary/10 border border-primary/20 flex items-center justify-center text-primary">
          <FileQuestion className="w-6 h-6 stroke-[1.8]" />
        </div>
        <p className="font-heading font-semibold text-base text-foreground">
          Расписание не опубликовано
        </p>
        <p className="text-xs text-muted-foreground max-w-xs">
          Учебная часть ещё не разместила расписание на этот день
        </p>
      </div>
    )
  }

  if (day.status === 'confirmed_empty' || day.lessons.length === 0) {
    return (
      <div className="glass-card rounded-2xl p-8 flex flex-col items-center justify-center text-center gap-3">
        <div className="w-12 h-12 rounded-2xl bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center text-emerald-400">
          <Sparkles className="w-6 h-6 stroke-[1.8]" />
        </div>
        <p className="font-heading font-semibold text-base text-foreground">Занятий нет</p>
        <p className="text-xs text-muted-foreground max-w-xs">
          В этот день пар не запланировано. Отличный повод отдохнуть!
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-3 pb-2">
      {day.stale && (
        <div className="glass-pill rounded-xl p-3 border-amber-500/30 bg-amber-500/10 text-amber-300 flex items-center gap-2.5 text-xs">
          <AlertTriangle className="w-4 h-4 flex-shrink-0 text-amber-400" />
          <span>Расписание может быть устаревшим (показана сохранённая копия).</span>
        </div>
      )}
      {day.lessons.map((lesson) => (
        <LessonCard key={lesson.number} lesson={lesson} reducedMotion={reducedMotion} />
      ))}
    </div>
  )
}
