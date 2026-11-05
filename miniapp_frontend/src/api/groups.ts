import { apiFetch } from './client'

export interface GroupItemDto {
  group_name?: string
  schedule_id?: number
  source_key?: string
  source_title?: string
  source_url?: string
  name?: string
  display_name?: string
}

export async function searchGroups(query: string): Promise<GroupItemDto[]> {
  const params = new URLSearchParams({ query })
  return apiFetch<GroupItemDto[]>(`/groups?${params}`)
}
