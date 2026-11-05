import {
  AlertCircle,
  ArrowRight,
  Ban,
  ChevronDown,
  Clock,
  MapPin,
  PlusCircle,
  Sparkles,
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
    cardClass: '',
    badge: null,
    badgeClass: '',
    icon: null,
  },
  added: {
    cardClass: 'border-emerald-500/30 bg-emerald-500/5',
    badge: 'Добавлена',
    badgeClass: 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/30',
    icon: PlusCircle,
  },
  cancelled: {
    cardClass: 'border-rose-500/30 bg-rose-500/5 opacity-60',
    badge: 'Отменена',
    badgeClass: 'bg-rose-500/15 text-rose-400 border border-rose-500/30',
    icon: Ban,
  },
  changed: {
    cardClass: 'border-amber-500/30 bg-amber-500/5',
    badge: 'Замена',
    badgeClass: 'bg-amber-500/15 text-amber-400 border border-amber-500/30',
    icon: Sparkles,
  },
}

function formatChangeKind(kind: string): string {
  switch (kind) {
    case 'classroom':
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
  const config = statusConfig[lesson.status] || statusConfig.unchanged
  const StatusIcon = config.icon

  return (
    <div
      className={cn(
        'glass-card rounded-2xl p-4 space-y-3 relative overflow-hidden transition-all duration-200',
        config.cardClass,
      )}
    >
      {/* Верхняя строка: номер пары, предмет и бейджи */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3 min-w-0">
          <div className="flex-shrink-0 w-8 h-8 rounded-xl bg-primary/15 border border-primary/25 flex items-center justify-center font-heading font-bold text-sm text-primary shadow-sm">
            {lesson.number}
          </div>
          <div className="min-w-0 pt-0.5">
            <h3
              className={cn(
                'font-heading font-bold text-base leading-snug tracking-tight text-foreground break-words',
                lesson.status === 'cancelled' && 'line-through opacity-70',
              )}
            >
              {lesson.subject}
            </h3>
          </div>
        </div>

        {/* Бейдж статуса */}
        <div className="flex items-center gap-1.5 flex-shrink-0">
          {config.badge && (
            <span
              className={cn(
                'inline-flex items-center gap-1 text-[11px] px-2.5 py-1 rounded-full font-medium tracking-tight',
                config.badgeClass,
              )}
            >
              {StatusIcon && <StatusIcon className="w-3 h-3" />}
              <span>
                {lesson.status === 'changed' && lesson.change
                  ? formatChangeKind(lesson.change.kind)
                  : config.badge}
              </span>
            </span>
          )}

          {lesson.status === 'changed' && lesson.change && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="p-1 rounded-lg hover:bg-white/10 dark:hover:bg-white/10 transition-colors"
              aria-label={expanded ? 'Скрыть детали' : 'Показать детали'}
            >
              <ChevronDown
                className={cn(
                  'w-4 h-4 text-muted-foreground transition-transform duration-200',
                  expanded ? 'rotate-180 text-primary' : '',
                  reducedMotion && 'transition-none',
                )}
              />
            </button>
          )}
        </div>
      </div>

      {/* Мета-информация: преподаватель, аудитория, время */}
      <div className="flex flex-wrap items-center gap-2 pt-0.5 text-xs">
        {lesson.classroom && (
          <div className="glass-pill rounded-lg px-2.5 py-1 flex items-center gap-1.5 text-foreground font-medium">
            <MapPin className="w-3.5 h-3.5 text-primary stroke-[2]" />
            <span>{lesson.classroom}</span>
          </div>
        )}

        {lesson.time_start && (
          <div className="glass-pill rounded-lg px-2.5 py-1 flex items-center gap-1.5 text-muted-foreground">
            <Clock className="w-3.5 h-3.5 text-muted-foreground stroke-[1.8]" />
            <span>{lesson.time_end ? `${lesson.time_start}–${lesson.time_end}` : lesson.time_start}</span>
          </div>
        )}

        {lesson.teacher && (
          <div className="glass-pill rounded-lg px-2.5 py-1 flex items-center gap-1.5 text-muted-foreground truncate max-w-[200px]">
            <User className="w-3.5 h-3.5 text-muted-foreground stroke-[1.8] flex-shrink-0" />
            <span className="truncate">{lesson.teacher}</span>
          </div>
        )}
      </div>

      {/* Раскрывающийся блок деталей замены */}
      {lesson.status === 'changed' && lesson.change && expanded && (
        <div
          className={cn(
            'border-t border-amber-500/20 pt-3 mt-2 space-y-2',
            reducedMotion ? '' : 'animate-in fade-in-0 duration-200',
          )}
        >
          <p className="text-[11px] font-semibold uppercase tracking-wider text-amber-500/80">
            Детали изменения
          </p>
          <ChangeDetail change={lesson.change} />
        </div>
      )}
    </div>
  )
}

function ChangeDetail({ change }: { change: NonNullable<LessonDto['change']> }) {
  const renderValue = (val: unknown): string => {
    if (val == null) return '—'
    if (typeof val === 'object') {
      const obj = val as Record<string, unknown>
      return (obj.subject || obj.classroom || obj.teacher || JSON.stringify(obj)) as string
    }
    return String(val)
  }

  if (change.before || change.after) {
    return (
      <div className="flex items-center gap-2 text-xs flex-wrap">
        {change.before && (
          <span className="glass-pill rounded-lg px-2.5 py-1 line-through text-muted-foreground bg-rose-500/10 border-rose-500/20">
            {renderValue(change.before)}
          </span>
        )}
        {change.before && change.after && (
          <ArrowRight className="w-3.5 h-3.5 text-amber-400 flex-shrink-0" />
        )}
        {change.after && (
          <span className="glass-pill rounded-lg px-2.5 py-1 text-emerald-400 font-semibold bg-emerald-500/15 border-emerald-500/30">
            {renderValue(change.after)}
          </span>
        )}
      </div>
    )
  }

  return (
    <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <AlertCircle className="w-3.5 h-3.5 text-amber-400" />
      <span>{formatChangeKind(change.kind)}</span>
    </div>
  )
}
