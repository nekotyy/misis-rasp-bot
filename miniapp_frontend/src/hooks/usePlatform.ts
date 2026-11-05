import { createContext, useContext } from 'react'
import { MockAdapter } from '../adapters/mock'
import type { PlatformAdapter } from '../adapters/platform'

const defaultAdapter = new MockAdapter()
export const PlatformContext = createContext<PlatformAdapter | null>(null)

/** Хук для получения платформенного адаптера */
export function usePlatform(): PlatformAdapter {
  const adapter = useContext(PlatformContext)
  return adapter ?? defaultAdapter
}
