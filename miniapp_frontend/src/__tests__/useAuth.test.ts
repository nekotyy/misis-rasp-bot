import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { PlatformAdapter } from '../adapters/platform'
import { useAuth } from '../hooks/useAuth'

vi.mock('../api/auth', () => ({
  authTelegram: vi.fn().mockResolvedValue({ token: 'test_token', account_id: 'acc_123' }),
  authVk: vi.fn().mockResolvedValue({ token: 'test_token_vk', account_id: 'acc_456' }),
}))

function createMockAdapter(overrides: Partial<PlatformAdapter> = {}): PlatformAdapter {
  return {
    platform: 'telegram',
    getInitData: () => '',
    getTheme: () => 'dark',
    getSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
    enableBackButton: vi.fn(),
    disableBackButton: vi.fn(),
    openLink: vi.fn(),
    hapticFeedback: vi.fn(),
    ready: vi.fn(),
    ...overrides,
  }
}

describe('useAuth - блокировка обычного браузера', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('переходит в unsupported_browser если открыто в обычном браузере без Telegram initData', async () => {
    const adapter = createMockAdapter({
      platform: 'telegram',
      getInitData: () => '',
    })

    const { result } = renderHook(() => useAuth(adapter))

    await waitFor(() => {
      expect(result.current.authState).toBe('unsupported_browser')
    })
    expect(result.current.error).toContain('Вход через обычный браузер запрещён')
  })

  it('переходит в unsupported_browser если открыто в обычном браузере без параметров запуска VK', async () => {
    const adapter = createMockAdapter({
      platform: 'vk',
      getInitData: () => '',
    })

    const { result } = renderHook(() => useAuth(adapter))

    await waitFor(() => {
      expect(result.current.authState).toBe('unsupported_browser')
    })
    expect(result.current.error).toContain('Вход через обычный браузер запрещён')
  })

  it('успешно авторизуется в Telegram при наличии initData', async () => {
    const adapter = createMockAdapter({
      platform: 'telegram',
      getInitData: () => 'query_id=AAHdF6IQAAAAAN0XohDhrOrc&user=%7B%22id%22%3A123%7D&hash=abc',
    })

    const { result } = renderHook(() => useAuth(adapter))

    await waitFor(() => {
      expect(result.current.authState).toBe('authenticated')
    })
    expect(result.current.accountId).toBe('acc_123')
  })

  it('успешно авторизуется во VK при наличии параметров запуска с подписью', async () => {
    const adapter = createMockAdapter({
      platform: 'vk',
      getInitData: () => 'vk_app_id=12345&vk_user_id=67890&sign=abcdef123',
    })

    const { result } = renderHook(() => useAuth(adapter))

    await waitFor(() => {
      expect(result.current.authState).toBe('authenticated')
    })
    expect(result.current.accountId).toBe('acc_456')
  })
})
