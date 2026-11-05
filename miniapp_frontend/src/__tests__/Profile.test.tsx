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

function renderProfile(props: Partial<React.ComponentProps<typeof ProfilePage>> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ProfilePage
        theme="dark"
        onThemeChange={vi.fn()}
        scale={110}
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
    renderProfile({ scale: 110 })
    expect(await screen.findByText('Масштаб интерфейса')).toBeDefined()
    expect(screen.getByText('110%')).toBeDefined()
  })

  it('отображает ползунок с правильными диапазонами', async () => {
    renderProfile({ scale: 110 })
    const slider = (await screen.findByLabelText('Ползунок масштаба интерфейса')) as HTMLInputElement
    expect(slider).toBeDefined()
    expect(slider.min).toBe('90')
    expect(slider.max).toBe('130')
    expect(slider.step).toBe('5')
    expect(slider.value).toBe('110')
  })

  it('вызывает onScaleChange при перемещении ползунка', async () => {
    const onScaleChange = vi.fn()
    renderProfile({ scale: 110, onScaleChange })
    const slider = await screen.findByLabelText('Ползунок масштаба интерфейса')
    fireEvent.change(slider, { target: { value: '125' } })
    expect(onScaleChange).toHaveBeenCalledWith(125)
  })

  it('вызывает onScaleChange при клике на быстрые пресеты масштаба', async () => {
    const onScaleChange = vi.fn()
    renderProfile({ scale: 110, onScaleChange })
    const preset125 = await screen.findByText('125%')
    fireEvent.click(preset125)
    expect(onScaleChange).toHaveBeenCalledWith(125)

    const preset90 = screen.getByText('90%')
    fireEvent.click(preset90)
    expect(onScaleChange).toHaveBeenCalledWith(90)
  })

  it('отображает кнопку сброса только если масштаб отличается от 110%', async () => {
    const onScaleChange = vi.fn()
    const { rerender } = renderProfile({ scale: 110, onScaleChange })
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
    expect(onScaleChange).toHaveBeenCalledWith(110)
  })
})
