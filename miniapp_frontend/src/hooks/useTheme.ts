import { useCallback, useEffect, useState } from 'react'
import type { PlatformAdapter } from '../adapters/platform'

export type Theme = 'light' | 'dark' | 'system'

const THEME_KEY = 'miniapp_theme'

function applyTheme(theme: Theme): void {
  const root = document.documentElement
  const eff = getEffectiveTheme(theme)
  root.setAttribute('data-theme', eff)
  root.setAttribute('data-theme-setting', theme)
  root.classList.toggle('dark', eff === 'dark')
  root.classList.toggle('light', eff === 'light')
}

function getEffectiveTheme(theme: Theme): 'light' | 'dark' {
  if (theme === 'system') {
    if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
    }
    return 'light'
  }
  return theme
}

function getReducedMotion(): boolean {
  if (typeof window !== 'undefined' && typeof window.matchMedia === 'function') {
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches
  }
  return false
}

/** Хук управления темой приложения */
export function useTheme(adapter?: PlatformAdapter) {
  const [theme, setThemeState] = useState<Theme>(() => {
    const saved = localStorage.getItem(THEME_KEY) as Theme | null
    if (saved && ['light', 'dark', 'system'].includes(saved)) return saved
    // Пробуем получить тему из адаптера платформы
    if (adapter) return adapter.getTheme()
    return 'system'
  })

  const [reducedMotion, setReducedMotion] = useState(getReducedMotion)
  const [effectiveTheme, setEffectiveTheme] = useState<'light' | 'dark'>(() =>
    getEffectiveTheme(theme),
  )

  useEffect(() => {
    applyTheme(theme)
    setEffectiveTheme(getEffectiveTheme(theme))
  }, [theme])

  useEffect(() => {
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = () => {
      if (theme === 'system') {
        setEffectiveTheme(getEffectiveTheme('system'))
        applyTheme('system')
      }
    }
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [theme])

  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    const handler = () => setReducedMotion(mq.matches)
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [])

  const setTheme = useCallback((next: Theme) => {
    localStorage.setItem(THEME_KEY, next)
    setThemeState(next)
  }, [])

  return { theme, setTheme, effectiveTheme, reducedMotion }
}
