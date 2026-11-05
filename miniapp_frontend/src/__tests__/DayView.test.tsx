import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { DayScheduleDto } from '../api/schedule'
import { DayView } from '../components/DayView'

const emptyDay: DayScheduleDto = {
  date: '2026-11-05',
  day_name: 'Среда',
  status: 'available',
  origin: 'schedule',
  stale: false,
  lessons: [],
}

const dayWithLessons: DayScheduleDto = {
  ...emptyDay,
  lessons: [
    {
      number: 1,
      subject: 'Физика',
      teacher: 'Петров П.П.',
      classroom: '204',
      status: 'unchanged',
    },
    {
      number: 2,
      subject: 'Химия',
      teacher: 'Сидорова А.В.',
      classroom: '105',
      status: 'added',
    },
  ],
}

describe('DayView', () => {
  it('показывает скелетон при загрузке', () => {
    const { container } = render(<DayView loading />)
    expect(container.querySelector('.animate-pulse')).toBeInTheDocument()
  })

  it('показывает «Занятий нет» для пустого дня', () => {
    render(<DayView day={emptyDay} />)
    expect(screen.getByText('Занятий нет')).toBeInTheDocument()
  })

  it('отображает список пар', () => {
    render(<DayView day={dayWithLessons} />)
    expect(screen.getByText('Физика')).toBeInTheDocument()
    expect(screen.getByText('Химия')).toBeInTheDocument()
  })

  it('показывает предупреждение для устаревших данных', () => {
    render(<DayView day={{ ...dayWithLessons, stale: true }} />)
    expect(screen.getByText(/устаревшим/)).toBeInTheDocument()
  })

  it('показывает «Расписание не опубликовано» для unpublished', () => {
    render(<DayView day={{ ...emptyDay, status: 'unpublished' }} />)
    expect(screen.getByText('Расписание не опубликовано')).toBeInTheDocument()
  })

  it('показывает «Занятий нет» для confirmed_empty', () => {
    render(<DayView day={{ ...emptyDay, status: 'confirmed_empty' }} />)
    expect(screen.getByText('Занятий нет')).toBeInTheDocument()
  })

  it('без day и без loading показывает «Нет данных»', () => {
    render(<DayView />)
    expect(screen.getByText('Нет данных')).toBeInTheDocument()
  })
})
