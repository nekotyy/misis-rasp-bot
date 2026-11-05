import {
  AlertTriangle,
  ArrowLeftRight,
  BookOpen,
  CheckCircle,
  ChevronDown,
  Clock,
  MapPin,
  User,
} from 'lucide-react'
import { useState } from 'react'
import type { LessonDto } from '../api/schedule'
import { cn } from '../lib/utils'

interface LessonCardProps {
  lesson: LessonDto
  reducedMotion?: boolean
}

const statusConfig = {
  unchanged: {
    className: 'bg-card border-border',
    badge: null,
    badgeClass: '',
  },
  added: {
    className: 'bg-green-50 border-green-200 dark:bg-green-950 dark:border-green-800',
    badge: 'Добавлена',
    badgeClass: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  },
  cancelled: {
    className: 'bg-muted/50 border-border opacity-60',
    badge: 'Отменена',
    badgeClass: 'bg-muted text-muted-foreground',
  },
  changed: {
    className: 'bg-amber-50 border-amber-200 dark:bg-amber-950 dark:border-amber-800',
    badge: 'Замена',
    badgeClass: 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200',
  },
}

function formatChangeKind(kind: string): string {
  switch (kind) {
    case 'room':
      return 'Другой кабинет'
    case 'teacher':
      return 'Другой преподаватель'
    case 'subject':
      return 'Другой предмет'
    default:
      return 'Замена'
  }
}

export function LessonCard({ lesson, reducedMotion = false }: LessonCardProps) {
  const [expanded, setExpanded] = useState(false)
  const config = statusConfig[lesson.status]

  return (
    <div
      className={cn(
        'rounded-xl border p-3 space-y-2',
        config.className,
        lesson.status === 'cancelled' ? 'line-through-titles' : '',
      )}
    >
      {/* Заголовок */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="flex-shrink-0 w-7 h-7 rounded-full bg-primary/10 flex items-center justify-center text-xs font-bold text-primary">
            {lesson.number}
          </span>
          <span
            className={cn(
              'font-semibold text-sm leading-tight',
              lesson.status === 'cancelled' && 'line-through text-muted-foreground',
            )}
          >
            {lesson.subject}
          </span>
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {config.badge && (
            <span className={cn('text-xs px-2 py-0.5 rounded-full font-medium', config.badgeClass)}>
              {lesson.status === 'changed' && lesson.change
                ? formatChangeKind(lesson.change.kind)
                : config.badge}
            </span>
          )}
          {lesson.status === 'changed' && lesson.change && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="p-1 rounded hover:bg-black/5 dark:hover:bg-white/10 transition-colors"
              aria-label={expanded ? 'Скрыть детали' : 'Показать детали'}
            >
              <ChevronDown
                className={cn(
                  'w-4 h-4 text-muted-foreground',
                  reducedMotion ? '' : 'transition-transform',
                  expanded ? 'rotate-180' : '',
                )}
              />
            </button>
          )}
        </div>
      </div>

      {/* Информация */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
        {lesson.teacher && (
          <div className="flex items-center gap-1">
            <User className="w-3 h-3" />
            <span>{lesson.teacher}</span>
          </div>
        )}
        {lesson.classroom && (
          <div className="flex items-center gap-1">
            <MapPin className="w-3 h-3" />
            <span>{lesson.classroom}</span>
          </div>
        )}
        {lesson.time_start && (
          <div className="flex items-center gap-1">
            <Clock className="w-3 h-3" />
            <span>
              {lesson.time_start}
              {lesson.time_end ? `–${lesson.time_end}` : ''}
            </span>
          </div>
        )}
      </div>

      {/* Детали замены */}
      {lesson.status === 'changed' && lesson.change && expanded && (
        <div
          className={cn(
            'border-t border-amber-200 dark:border-amber-800 pt-2 space-y-1',
            reducedMotion ? '' : 'animate-in fade-in-0 duration-150',
          )}
        >
          <ChangeDetail change={lesson.change} />
        </div>
      )}
    </div>
  )
}

function ChangeDetail({ change }: { change: NonNullable<LessonDto['change']> }) {
  const renderValue = (val: unknown): string => {
    if (val == null) return '—'
    if (typeof val === 'string' || typeof val === 'number') return String(val)
    return JSON.stringify(val)
  }

  if (change.before || change.after) {
    return (
      <div className="flex items-center gap-2 text-xs flex-wrap">
        {change.before && (
          <span className="flex items-center gap-1 text-muted-foreground">
            <BookOpen className="w-3 h-3" />
            <span>{renderValue(change.before)}</span>
          </span>
        )}
        {change.before && change.after && (
          <ArrowLeftRight className="w-3 h-3 text-amber-500" />
        )}
        {change.after && (
          <span className="flex items-center gap-1 text-foreground font-medium">
            <CheckCircle className="w-3 h-3 text-green-500" />
            <span>{renderValue(change.after)}</span>
          </span>
        )}
      </div>
    )
  }

  return (
    <div className="flex items-center gap-1 text-xs text-muted-foreground">
      <AlertTriangle className="w-3 h-3" />
      <span>{change.kind}</span>
    </div>
  )
}

