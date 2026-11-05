import { useCallback, useEffect, useState } from 'react'
import { authTelegram, authVk } from '../api/auth'
import { clearToken, setToken, setUnauthorizedHandler } from '../api/client'
import type { PlatformAdapter } from '../adapters/platform'

export type AuthState = 'idle' | 'loading' | 'authenticated' | 'error' | 'unsupported_browser'

/** Хук инициализации авторизации и хранения токена в памяти */
export function useAuth(adapter: PlatformAdapter) {
  const [authState, setAuthState] = useState<AuthState>('idle')
  const [accountId, setAccountId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const authenticate = useCallback(async () => {
    setAuthState('loading')
    setError(null)

    let initData = adapter.getInitData()
    const isDevExplicit =
      Boolean(import.meta.env.DEV) &&
      (import.meta.env.VITE_MOCK_PLATFORM === 'true' ||
        (typeof window !== 'undefined' &&
          new URLSearchParams(window.location.search).get('dev') === '1'))

    // Защита от race condition: Telegram WebApp API может инициализироваться с задержкой в несколько миллисекунд
    if (adapter.platform === 'telegram' && (!initData || initData.trim() === '')) {
      for (let i = 0; i < 8; i++) {
        await new Promise((resolve) => setTimeout(resolve, 50))
        initData = adapter.getInitData()
        if (initData && initData.trim() !== '') break
      }
    }

    // Запрет входа через обычный браузер:
    // 1. В обычном браузере TelegramAdapter.getInitData() возвращает пустую строку
    // 2. В обычном браузере VkAdapter не находит параметров vk_app_id/sign в строке запроса
    // 3. MockAdapter доступен исключительно при явном флаге ?dev=1 в режиме разработки
    if (adapter.platform === 'mock' && !isDevExplicit) {
      setAuthState('unsupported_browser')
      setError('Вход через обычный браузер запрещён. Откройте приложение через Telegram или ВКонтакте.')
      return
    }

    if (adapter.platform === 'telegram' && (!initData || initData.trim() === '')) {
      setAuthState('unsupported_browser')
      setError('Вход через обычный браузер запрещён. Откройте приложение через Telegram или ВКонтакте.')
      return
    }

    if (adapter.platform === 'vk') {
      const hasVkParams =
        Boolean(initData) &&
        (initData.includes('vk_app_id=') || initData.includes('sign='))
      if (!hasVkParams) {
        setAuthState('unsupported_browser')
        setError('Вход через обычный браузер запрещён. Откройте приложение через Telegram или ВКонтакте.')
        return
      }
    }

    try {
      let result
      if (adapter.platform === 'telegram') {
        result = await authTelegram(initData)
      } else if (adapter.platform === 'vk') {
        result = await authVk(initData)
      } else if (isDevExplicit) {
        // Mock режим разработки с явным флагом
        try {
          result = await authTelegram(initData || 'dev:10001:dev_student')
        } catch {
          result = { token: 'mock_token', account_id: 'mock_user' }
          setToken(result.token)
        }
      } else {
        setAuthState('unsupported_browser')
        return
      }
      setAccountId(result.account_id)
      setAuthState('authenticated')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Ошибка авторизации')
      setAuthState('error')
    }
  }, [adapter])

  useEffect(() => {
    setUnauthorizedHandler(() => {
      clearToken()
      setAuthState('idle')
      setAccountId(null)
      authenticate()
    })
    authenticate()
  }, [authenticate])

  return { authState, accountId, error, retry: authenticate }
}
