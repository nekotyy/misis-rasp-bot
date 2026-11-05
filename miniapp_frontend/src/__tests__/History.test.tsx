import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as historyApi from '../api/history'
import { HistoryPage } from '../pages/History'

vi.mock('../api/history')

function renderWithClient(ui: React.ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('HistoryPage - навигация понедельник-суббота', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('отображает диапазон недели с понедельника по субботу (2 нояб. – 7 нояб.)', async () => {
    vi.mocked(historyApi.fetchHistory).mockResolvedValue({
      subscription_id: 1,
      subscription_title: 'ИСП-25-1',
      week_start: '2026-11-02',
      days: [
        {
          date: '2026-11-02',
          day_name: 'Понедельник',
          status: 'available',
          stale: false,
          origin: 'archive',
          lessons: [],
        },
      ],
      has_previous_week: true,
      has_next_week: false,
      bounds: { start: '2026-08-01', end: '2026-11-07' },
    })

    renderWithClient(
      <HistoryPage
        subscriptionId={1}
        subscriptionTitle="ИСП-25-1"
        subscriptions={[{ id: 1, group_name: 'ИСП-25-1', is_primary: true }]}
      />,
    )

    // Должен отобразиться диапазон с понедельника 2 ноября по субботу 7 ноября (без воскресенья 8 ноября)
    await waitFor(() => {
      expect(screen.getByText(/2 нояб\. – 7 нояб\./i)).toBeInTheDocument()
    })
  })

  it('при нажатии назад перемещается ровно на 7 дней (26 окт), а не перепрыгивает на 19 окт', async () => {
    vi.mocked(historyApi.fetchHistory).mockImplementation(async (_subId, weekStart) => {
      if (weekStart === '2026-10-26') {
        return {
          subscription_id: 1,
          subscription_title: 'ИСП-25-1',
          week_start: '2026-10-26',
          days: [],
          has_previous_week: true,
          has_next_week: true,
          bounds: { start: '2026-08-01', end: '2026-11-07' },
        }
      }
      return {
        subscription_id: 1,
        subscription_title: 'ИСП-25-1',
        week_start: '2026-11-02',
        days: [],
        has_previous_week: true,
        has_next_week: false,
        bounds: { start: '2026-08-01', end: '2026-11-07' },
      }
    })

    const user = userEvent.setup()
    renderWithClient(
      <HistoryPage
        subscriptionId={1}
        subscriptionTitle="ИСП-25-1"
        subscriptions={[{ id: 1, group_name: 'ИСП-25-1', is_primary: true }]}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText(/2 нояб\. – 7 нояб\./i)).toBeInTheDocument()
    })

    const prevButton = screen.getByLabelText('Предыдущая неделя')
    await user.click(prevButton)

    // Проверяем, что запрос ушёл ровно на 2026-10-26 (а не 2026-10-25 или 2026-10-19)
    await waitFor(() => {
      expect(historyApi.fetchHistory).toHaveBeenCalledWith(1, '2026-10-26')
      expect(screen.getByText(/26 окт\. – 31 окт\./i)).toBeInTheDocument()
    })
  })

  it('при нажатии вперед возвращается на следующую неделю (2 нояб), а не застревает', async () => {
    vi.mocked(historyApi.fetchHistory).mockImplementation(async (_subId, weekStart) => {
      if (weekStart === '2026-11-02') {
        return {
          subscription_id: 1,
          subscription_title: 'ИСП-25-1',
          week_start: '2026-11-02',
          days: [],
          has_previous_week: true,
          has_next_week: false,
          bounds: { start: '2026-08-01', end: '2026-11-07' },
        }
      }
      return {
        subscription_id: 1,
        subscription_title: 'ИСП-25-1',
        week_start: '2026-10-26',
        days: [],
        has_previous_week: true,
        has_next_week: true,
        bounds: { start: '2026-08-01', end: '2026-11-07' },
      }
    })

    const user = userEvent.setup()
    renderWithClient(
      <HistoryPage
        subscriptionId={1}
        subscriptionTitle="ИСП-25-1"
        subscriptions={[{ id: 1, group_name: 'ИСП-25-1', is_primary: true }]}
      />,
    )

    await waitFor(() => {
      expect(screen.getByText(/26 окт\. – 31 окт\./i)).toBeInTheDocument()
    })

    const nextButton = screen.getByLabelText('Следующая неделя')
    await user.click(nextButton)

    await waitFor(() => {
      expect(historyApi.fetchHistory).toHaveBeenCalledWith(1, '2026-11-02')
      expect(screen.getByText(/2 нояб\. – 7 нояб\./i)).toBeInTheDocument()
    })
  })
})
