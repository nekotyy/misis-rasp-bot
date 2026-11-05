import { BookX, Calendar } from 'lucide-react'
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
      <div className="flex flex-col items-center justify-center py-12 gap-3 text-muted-foreground">
        <Calendar className="w-10 h-10 opacity-30" />
        <p className="text-sm">Нет данных</p>
      </div>
    )
  }

  if (day.status === 'unpublished') {
    return (
      <div className="flex flex-col items-center justify-center py-12 gap-3 text-muted-foreground">
        <Calendar className="w-10 h-10 opacity-30" />
        <p className="text-sm font-medium">Расписание не опубликовано</p>
        <p className="text-xs text-center px-6">Расписание на этот день ещё не опубликовано</p>
      </div>
    )
  }

  if (day.status === 'confirmed_empty') {
    return (
      <div className="flex flex-col items-center justify-center py-12 gap-3 text-muted-foreground">
        <BookX className="w-10 h-10 opacity-30" />
        <p className="text-sm font-medium">Занятий нет</p>
        <p className="text-xs text-center px-6">В этот день занятий не запланировано</p>
      </div>
    )
  }

  if (day.lessons.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-12 gap-3 text-muted-foreground">
        <BookX className="w-10 h-10 opacity-30" />
        <p className="text-sm font-medium">Занятий нет</p>
      </div>
    )
  }

  return (
    <div className="space-y-2 pb-2">
      {day.stale && (
        <div className="bg-yellow-50 dark:bg-yellow-950 border border-yellow-200 dark:border-yellow-800 rounded-lg p-2 text-xs text-yellow-800 dark:text-yellow-200 flex items-center gap-2">
          <span>⚠️</span>
          <span>Расписание может быть устаревшим</span>
        </div>
      )}
      {day.lessons.map((lesson) => (
        <LessonCard key={lesson.number} lesson={lesson} reducedMotion={reducedMotion} />
      ))}
    </div>
  )
}
