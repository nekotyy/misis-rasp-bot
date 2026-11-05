import { createContext, useContext } from 'react'
import type { PlatformAdapter } from '../adapters/platform'

export const PlatformContext = createContext<PlatformAdapter | null>(null)

/** Хук для получения платформенного адаптера */
export function usePlatform(): PlatformAdapter {
  const adapter = useContext(PlatformContext)
  if (!adapter) {
    throw new Error('usePlatform must be used within PlatformProvider')
  }
  return adapter
}
