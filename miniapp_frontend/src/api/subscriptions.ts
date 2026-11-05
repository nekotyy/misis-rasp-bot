import { apiFetch } from './client'
import type { SubscriptionDto } from './profile'

export interface AddSubscriptionRequest {
  group_name?: string
  schedule_id?: number
  subscription_key?: string
  subscription_title?: string
  subscription_type?: string
  university_subgroup?: number
  is_primary?: boolean
  notifications_enabled?: boolean
  notify_enabled?: boolean
}

export interface UpdateSubscriptionRequest {
  is_primary?: boolean
  university_subgroup?: number
  notifications_enabled?: boolean
  notify_enabled?: boolean
}

export async function addSubscription(req: AddSubscriptionRequest): Promise<SubscriptionDto> {
  const payload = {
    ...req,
    notifications_enabled: req.notifications_enabled ?? req.notify_enabled,
  }
  return apiFetch<SubscriptionDto>('/me/subscriptions', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export async function updateSubscription(
  id: number,
  req: UpdateSubscriptionRequest,
): Promise<SubscriptionDto> {
  const payload = {
    ...req,
    notifications_enabled: req.notifications_enabled ?? req.notify_enabled,
  }
  return apiFetch<SubscriptionDto>(`/me/subscriptions/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

export async function deleteSubscription(id: number): Promise<void> {
  await apiFetch<void>(`/me/subscriptions/${id}`, { method: 'DELETE' })
}
