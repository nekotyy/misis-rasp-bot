import { apiFetch, setToken } from './client'

export interface AuthResponse {
  token: string
  account_id: string
}

export async function authTelegram(initData: string): Promise<AuthResponse> {
  const data = await apiFetch<AuthResponse>('/auth/telegram', {
    method: 'POST',
    body: JSON.stringify({ init_data: initData }),
  })
  setToken(data.token)
  return data
}

export async function authVk(
  launchParams: string,
  extra?: { firstName?: string; lastName?: string; photoUrl?: string },
): Promise<AuthResponse> {
  const data = await apiFetch<AuthResponse>('/auth/vk', {
    method: 'POST',
    body: JSON.stringify({
      launch_params: launchParams,
      first_name: extra?.firstName,
      last_name: extra?.lastName,
      photo_url: extra?.photoUrl,
    }),
  })
  setToken(data.token)
  return data
}

export async function authLogout(): Promise<void> {
  await apiFetch('/auth/logout', { method: 'POST' })
}
