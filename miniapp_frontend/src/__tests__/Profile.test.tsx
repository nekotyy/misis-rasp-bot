import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ProfilePage } from '../pages/Profile'

vi.mock('../api/profile', () => ({
  fetchProfile: vi.fn().mockResolvedValue({
    account_id: 'acc-1',
    current_identity: {
      platform: 'telegram',
      user_id: 12345,
      username: 'test_user',
    },
    identities: [
      {
        platform: 'telegram',
        user_id: 12345,
        username: 'test_user',
        photo_url: '/api/miniapp/v1/avatar/telegram/12345?v=unique123',
      },
    ],
    subscriptions: [],
    preferences: {
      notify_enabled: true,
      notifications_enabled: true,
    },
  }),
  createLinkRequest: vi.fn(),
  confirmLinkRequest: vi.fn(),
  unlinkIdentity: vi.fn(),
  updatePreferences: vi.fn(),
}))

vi.mock('../api/groups', () => ({
  searchGroups: vi.fn().mockResolvedValue([
    { group_name: 'БПИ22-1', schedule_id: 1, source_title: 'БПИ22-1' },
    { group_name: 'БПИ22-2', schedule_id: 2, source_title: 'БПИ22-2' },
  ]),
}))

vi.mock('../api/subscriptions', () => ({
  addSubscription: vi.fn().mockResolvedValue({ id: 10, group_name: 'БПИ22-2', is_primary: false }),
  updateSubscription: vi.fn().mockResolvedValue({ id: 1, is_primary: true }),
  deleteSubscription: vi.fn().mockResolvedValue(undefined),
}))

function renderProfile(props: Partial<React.ComponentProps<typeof ProfilePage>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ProfilePage
        theme="dark"
        onThemeChange={vi.fn()}
        scale={100}
        onScaleChange={vi.fn()}
        {...props}
      />
    </QueryClientProvider>,
  )
}

describe('ProfilePage - Масштаб интерфейса', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('отображает секцию масштабирования интерфейса и текущий процент', async () => {
    renderProfile({ scale: 100 })
    expect(await screen.findByText('Масштаб интерфейса')).toBeDefined()
    expect(screen.getByText('100%')).toBeDefined()
  })

  it('отображает ползунок с правильными диапазонами', async () => {
    renderProfile({ scale: 100 })
    const slider = (await screen.findByLabelText('Ползунок масштаба интерфейса')) as HTMLInputElement
    expect(slider).toBeDefined()
    expect(slider.min).toBe('90')
    expect(slider.max).toBe('130')
    expect(slider.step).toBe('5')
    expect(slider.value).toBe('100')
  })

  it('вызывает onScaleChange при перемещении ползунка', async () => {
    const onScaleChange = vi.fn()
    renderProfile({ scale: 100, onScaleChange })
    const slider = await screen.findByLabelText('Ползунок масштаба интерфейса')
    fireEvent.change(slider, { target: { value: '125' } })
    expect(onScaleChange).toHaveBeenCalledWith(125)
  })

  it('вызывает onScaleChange при клике на быстрые пресеты масштаба', async () => {
    const onScaleChange = vi.fn()
    renderProfile({ scale: 100, onScaleChange })
    const preset125 = await screen.findByText('125%')
    fireEvent.click(preset125)
    expect(onScaleChange).toHaveBeenCalledWith(125)

    const preset90 = screen.getByText('90%')
    fireEvent.click(preset90)
    expect(onScaleChange).toHaveBeenCalledWith(90)
  })

  it('отображает кнопку сброса только если масштаб отличается от 100%', async () => {
    const onScaleChange = vi.fn()
    const { rerender } = renderProfile({ scale: 100, onScaleChange })
    await screen.findByText('Масштаб интерфейса')
    expect(screen.queryByLabelText('Сбросить масштаб')).toBeNull()

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    rerender(
      <QueryClientProvider client={queryClient}>
        <ProfilePage
          theme="dark"
          onThemeChange={vi.fn()}
          scale={125}
          onScaleChange={onScaleChange}
        />
      </QueryClientProvider>,
    )

    const resetBtn = screen.getByLabelText('Сбросить масштаб')
    expect(resetBtn).toBeDefined()
    fireEvent.click(resetBtn)
    expect(onScaleChange).toHaveBeenCalledWith(100)
  })
})

describe('ProfilePage - Добавление групп', () => {
  it('открывает форму поиска группы по кнопке Добавить группу', async () => {
    renderProfile()
    const addBtn = await screen.findByText('Добавить группу')
    fireEvent.click(addBtn)

    expect(await screen.findByText('Поиск и добавление группы')).toBeDefined()
    expect(screen.getByPlaceholderText('Введите название группы (напр. ИСП-25-1)...')).toBeDefined()
  })

  it('выполняет поиск и отображает результаты', async () => {
    renderProfile()
    const addBtn = await screen.findByText('Добавить группу')
    fireEvent.click(addBtn)

    const input = screen.getByPlaceholderText('Введите название группы (напр. ИСП-25-1)...')
    fireEvent.change(input, { target: { value: 'БПИ' } })

    expect(await screen.findByText('БПИ22-1')).toBeDefined()
    expect(screen.getByText('БПИ22-2')).toBeDefined()
  })

  it('отображает @username и компактную таблетку Текущий', async () => {
    renderProfile()
    expect(await screen.findByText('@test_user')).toBeDefined()
    const badge = screen.getByText('Текущий')
    expect(badge).toBeDefined()
    expect(badge.className).toContain('text-[10px]')
  })

  it('отображает аватар пользователя с приоритетом ident.photo_url', async () => {
    renderProfile()
    const img = (await screen.findByAltText('test_user')) as HTMLImageElement
    expect(img).toBeDefined()
    expect(img.src).toContain('/api/miniapp/v1/avatar/telegram/12345?v=unique123')
  })
})
