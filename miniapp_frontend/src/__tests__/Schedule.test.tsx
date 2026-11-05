import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { SubscriptionDto } from '../api/profile'
import { SchedulePage } from '../pages/Schedule'

vi.mock('../api/schedule', () => ({
  fetchSchedule: vi.fn().mockResolvedValue({
    days: [
      {
        date: '2026-11-05',
        day_name: 'Четверг',
        status: 'available',
        lessons: [
          {
            number: 1,
            subject: 'Информатика',
            teacher: 'Иванов И.И.',
            classroom: '302',
            time_start: '09:00',
            time_end: '10:30',
            status: 'unchanged',
          },
        ],
      },
      {
        date: '2026-11-06',
        day_name: 'Пятница',
        status: 'available',
        lessons: [],
      },
      {
        date: '2026-11-07',
        day_name: 'Суббота',
        status: 'available',
        lessons: [],
      },
    ],
  }),
}))

const mockSubs: SubscriptionDto[] = [
  {
    id: 1,
    group_name: 'БПИ22-1',
    is_primary: true,
  },
  {
    id: 2,
    group_name: 'БПИ22-2',
    is_primary: false,
  },
]

function renderSchedule(props: Partial<React.ComponentProps<typeof SchedulePage>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={queryClient}>
        <SchedulePage
          subscriptionId={1}
          subscriptionTitle="БПИ22-1"
          subscriptions={mockSubs}
          onSelectSubscription={vi.fn()}
          {...props}
        />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('SchedulePage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('отображает пустой экран при отсутствии подписки с кнопкой выбора', () => {
    renderSchedule({ subscriptionId: undefined, subscriptionTitle: undefined, subscriptions: [] })
    expect(screen.getByText('Нет подписок')).toBeDefined()
    expect(screen.getByText('Выбрать группу')).toBeDefined()
  })

  it('отображает заголовок группы и дни расписания', async () => {
    renderSchedule()
    expect(await screen.findByText('БПИ22-1')).toBeDefined()
    expect(await screen.findByText('Информатика')).toBeDefined()
  })

  it('показывает кнопку переключения групп если подписок больше одной', async () => {
    const onSelect = vi.fn()
    renderSchedule({ onSelectSubscription: onSelect })

    const groupBtn = await screen.findByText('БПИ22-1')
    fireEvent.click(groupBtn)

    expect(await screen.findByText('Выберите группу')).toBeDefined()
    expect(screen.getByText('БПИ22-2')).toBeDefined()

    fireEvent.click(screen.getByText('БПИ22-2'))
    expect(onSelect).toHaveBeenCalledWith(2)
  })
})
