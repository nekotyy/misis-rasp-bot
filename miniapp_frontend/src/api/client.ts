/** Токен авторизации хранится только в памяти */
let _token: string | null = null
let _onUnauthorized: (() => void) | null = null

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? '/api/miniapp/v1'

export function setToken(token: string): void {
  _token = token
}

export function clearToken(): void {
  _token = null
}

export function getToken(): string | null {
  return _token
}

export function setUnauthorizedHandler(handler: () => void): void {
  _onUnauthorized = handler
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(options.headers as Record<string, string>),
  }

  if (_token) {
    headers['Authorization'] = `Bearer ${_token}`
  }

  const response = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers,
  })

  if (response.status === 401) {
    clearToken()
    _onUnauthorized?.()
    throw new Error('Unauthorized')
  }

  if (!response.ok) {
    const text = await response.text().catch(() => '')
    throw new Error(`HTTP ${response.status}: ${text}`)
  }

  if (response.status === 204) {
    return undefined as unknown as T
  }

  return response.json() as Promise<T>
}
