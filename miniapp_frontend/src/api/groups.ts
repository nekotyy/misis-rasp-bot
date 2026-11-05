import { apiFetch } from './client'

export interface GroupItemDto {
  name: string
  display_name?: string
}

export async function searchGroups(query: string): Promise<GroupItemDto[]> {
  const params = new URLSearchParams({ query })
  return apiFetch<GroupItemDto[]>(`/groups?${params}`)
}
