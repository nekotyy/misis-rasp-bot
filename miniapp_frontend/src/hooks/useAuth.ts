import { useCallback, useEffect, useState } from 'react'
import { authTelegram, authVk } from '../api/auth'
import { clearToken, setToken, setUnauthorizedHandler } from '../api/client'
import type { PlatformAdapter } from '../adapters/platform'

export type AuthState = 'idle' | 'loading' | 'authenticated' | 'error'

/** Хук инициализации авторизации и хранения токена в памяти */
export function useAuth(adapter: PlatformAdapter) {
  const [authState, setAuthState] = useState<AuthState>('idle')
  const [accountId, setAccountId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const authenticate = useCallback(async () => {
    setAuthState('loading')
    setError(null)
    try {
      const initData = adapter.getInitData()
      let result
      if (adapter.platform === 'telegram') {
        result = await authTelegram(initData)
      } else if (adapter.platform === 'vk') {
        result = await authVk(initData)
      } else {
        // Mock режим: сначала пробуем dev-авторизацию на сервере (если запущен), иначе offline mock
        try {
          result = await authTelegram(initData || 'dev:10001:dev_student')
        } catch {
          result = { token: 'mock_token', account_id: 'mock_user' }
          setToken(result.token)
        }
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
