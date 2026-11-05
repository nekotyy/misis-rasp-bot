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
    cardClass: 'border-success/40 bg-success-surface/30',
    badge: 'Добавлена',
    badgeClass: 'bg-success-surface text-success border border-success/40',
    icon: PlusCircle,
  },
  cancelled: {
    cardClass: 'border-destructive/40 bg-destructive-surface/30 opacity-70',
    badge: 'Отменена',
    badgeClass: 'bg-destructive-surface text-destructive border border-destructive/40',
    icon: Ban,
  },
  changed: {
    cardClass: 'border-primary/40 bg-primary-surface/40',
    badge: 'Замена',
    badgeClass: 'bg-primary-surface text-primary border border-primary/40',
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
          <div className="flex-shrink-0 w-9 h-9 rounded-xl bg-primary text-white flex items-center justify-center font-display font-black text-base shadow-sm">
            {lesson.number}
          </div>
          <div className="min-w-0 pt-0.5">
            <h3
              className={cn(
                'font-heading font-bold text-lg leading-snug tracking-tight text-foreground break-words',
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
                'font-heading inline-flex items-center gap-1.5 text-xs px-3 py-1 rounded-full font-bold tracking-tight shadow-sm',
                config.badgeClass,
              )}
            >
              {StatusIcon && <StatusIcon className="w-3.5 h-3.5" />}
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
              className="p-1 rounded-lg hover:bg-neutral-surface transition-colors cursor-pointer"
              aria-label={expanded ? 'Скрыть детали' : 'Показать детали'}
            >
              <ChevronDown
                className={cn(
                  'w-4 h-4 text-neutral transition-transform duration-200',
                  expanded ? 'rotate-180 text-primary' : '',
                  reducedMotion && 'transition-none',
                )}
              />
            </button>
          )}
        </div>
      </div>

      {/* Мета-информация: преподаватель, аудитория, время */}
      <div className="flex flex-wrap items-center gap-2 pt-0.5 text-sm">
        {lesson.classroom && (
          <div className="glass-pill rounded-lg px-3 py-1.5 flex items-center gap-1.5 text-foreground bg-primary-surface/40 border-primary/20 font-semibold">
            <MapPin className="w-4 h-4 text-primary stroke-[2.2]" />
            <span>{lesson.classroom}</span>
          </div>
        )}

        {lesson.time_start && (
          <div className="glass-pill rounded-lg px-3 py-1.5 flex items-center gap-1.5 text-neutral font-medium">
            <Clock className="w-4 h-4 text-neutral stroke-[2]" />
            <span>{lesson.time_end ? `${lesson.time_start}–${lesson.time_end}` : lesson.time_start}</span>
          </div>
        )}

        {lesson.teacher && (
          <div className="glass-pill rounded-lg px-3 py-1.5 flex items-center gap-1.5 text-neutral font-medium break-words">
            <User className="w-4 h-4 text-neutral stroke-[2] flex-shrink-0" />
            <span>{lesson.teacher}</span>
          </div>
        )}
      </div>

      {/* Раскрывающийся блок деталей замены */}
      {lesson.status === 'changed' && lesson.change && expanded && (
        <div
          className={cn(
            'border-t border-primary/30 pt-3 mt-2 space-y-2',
            reducedMotion ? '' : 'animate-in fade-in-0 duration-200',
          )}
        >
          <p className="font-heading text-xs font-bold uppercase tracking-wider text-primary">
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
      <div className="flex items-center gap-2 text-sm flex-wrap">
        {change.before && (
          <span className="glass-pill rounded-lg px-3 py-1.5 line-through text-destructive bg-destructive-surface border border-destructive/30">
            {renderValue(change.before)}
          </span>
        )}
        {change.before && change.after && (
          <ArrowRight className="w-4 h-4 text-primary flex-shrink-0" />
        )}
        {change.after && (
          <span className="glass-pill rounded-lg px-3 py-1.5 text-success font-semibold bg-success-surface border border-success/30">
            {renderValue(change.after)}
          </span>
        )}
      </div>
    )
  }

  return (
    <div className="flex items-center gap-2 text-sm text-neutral">
      <AlertCircle className="w-4 h-4 text-primary" />
      <span>{formatChangeKind(change.kind)}</span>
    </div>
  )
}
