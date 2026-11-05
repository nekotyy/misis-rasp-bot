import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { LessonDto } from '../api/schedule'
import { LessonCard } from '../components/LessonCard'

const baseLesson: LessonDto = {
  number: 1,
  subject: 'Математика',
  teacher: 'Иванов И.И.',
  classroom: '301',
  time_start: '09:00',
  time_end: '10:30',
  status: 'unchanged',
}

describe('LessonCard', () => {
  it('отображает урок со статусом unchanged', () => {
    render(<LessonCard lesson={baseLesson} />)
    expect(screen.getByText('Математика')).toBeInTheDocument()
    expect(screen.getByText('Иванов И.И.')).toBeInTheDocument()
    expect(screen.getByText('301')).toBeInTheDocument()
    expect(screen.getByText('09:00–10:30')).toBeInTheDocument()
  })

  it('отображает урок со статусом added с меткой «Добавлена»', () => {
    render(<LessonCard lesson={{ ...baseLesson, status: 'added' }} />)
    expect(screen.getByText('Добавлена')).toBeInTheDocument()
  })

  it('отображает урок со статусом cancelled', () => {
    render(<LessonCard lesson={{ ...baseLesson, status: 'cancelled' }} />)
    expect(screen.getByText('Отменена')).toBeInTheDocument()
  })

  it('отображает урок со статусом changed с кнопкой раскрытия', () => {
    const lesson: LessonDto = {
      ...baseLesson,
      status: 'changed',
      change: {
        kind: 'room',
        before: { classroom: '101' },
        after: { classroom: '301' },
      },
    }
    render(<LessonCard lesson={lesson} />)
    expect(screen.getByText('Другой кабинет')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /показать детали/i })).toBeInTheDocument()
  })

  it('урок без времени не показывает время', () => {
    const lesson = { ...baseLesson, time_start: undefined, time_end: undefined }
    render(<LessonCard lesson={lesson} />)
    expect(screen.queryByText(/09:00/)).not.toBeInTheDocument()
  })

  it('урок без преподавателя рендерится без ошибок', () => {
    render(<LessonCard lesson={{ ...baseLesson, teacher: '' }} />)
    expect(screen.getByText('Математика')).toBeInTheDocument()
  })

  it('номер пары отображается', () => {
    render(<LessonCard lesson={baseLesson} />)
    expect(screen.getByText('1')).toBeInTheDocument()
  })

  it('changed без change.kind показывает generic метку', () => {
    render(<LessonCard lesson={{ ...baseLesson, status: 'changed', change: { kind: 'unknown_type' } }} />)
    expect(screen.getByText('Замена')).toBeInTheDocument()
  })
})
