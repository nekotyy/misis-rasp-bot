import { apiFetch } from './client'

export interface IdentityDto {
  platform: string
  user_id: number
  username?: string | null
  full_name?: string | null
  display_name?: string
  photo_url?: string | null
}

export interface SubscriptionDto {
  id: number
  subscription_type?: string
  subscription_key?: string
  subscription_title?: string | null
  subscription_url?: string | null
  group_name?: string | null
  schedule_id?: number | null
  university_subgroup?: number
  is_primary: boolean
  notifications_enabled?: boolean
  notify_enabled?: boolean
}

export interface PreferencesDto {
  theme?: string
  accent?: string
  density?: string
  reduced_motion?: boolean
  notifications_enabled?: boolean
  notify_enabled?: boolean
}

export interface ProfileResponse {
  account_id: string
  current_identity: IdentityDto
  identities: IdentityDto[]
  subscriptions: SubscriptionDto[]
  preferences: PreferencesDto
}

export interface PreferencesUpdateRequest {
  theme?: 'system' | 'light' | 'dark'
  accent?: string
  density?: 'comfortable' | 'compact'
  reduced_motion?: boolean
  notifications_enabled?: boolean
  notify_enabled?: boolean
}

export async function fetchProfile(): Promise<ProfileResponse> {
  return apiFetch<ProfileResponse>('/me')
}

export async function updatePreferences(update: PreferencesUpdateRequest): Promise<ProfileResponse> {
  const payload = {
    ...update,
    notifications_enabled: update.notifications_enabled ?? update.notify_enabled,
  }
  return apiFetch<ProfileResponse>('/me/preferences', {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export async function createLinkRequest(): Promise<{ token: string; expires_at: string }> {
  return apiFetch<{ token: string; expires_at: string }>('/account-links', {
    method: 'POST',
  })
}

export async function confirmLinkRequest(token: string): Promise<{ success: boolean }> {
  return apiFetch<{ success: boolean }>('/account-links/confirm', {
    method: 'POST',
    body: JSON.stringify({ token, primary_choice: 'caller' }),
  })
}

export async function unlinkIdentity(platform: string): Promise<{ success: boolean; new_account_id?: string }> {
  return apiFetch<{ success: boolean; new_account_id?: string }>(`/me/identities/${encodeURIComponent(platform)}`, {
    method: 'DELETE',
  })
}
