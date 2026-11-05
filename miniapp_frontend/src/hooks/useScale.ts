import { useCallback, useEffect, useState } from 'react'

/** Константы масштаба интерфейса */
export const DEFAULT_SCALE = 110 // По умолчанию увеличенный (110%)
export const MIN_SCALE = 90
export const MAX_SCALE = 130
export const SCALE_STEP = 5

export const SCALE_STORAGE_KEY = 'miniapp_ui_scale'

/** Ограничение значения масштаба в допустимый диапазон */
export function clampScale(value: number): number {
  if (Number.isNaN(value)) return DEFAULT_SCALE
  return Math.min(Math.max(Math.round(value), MIN_SCALE), MAX_SCALE)
}

/** Применение масштаба к корневому элементу документа через font-size */
export function applyScale(scale: number): void {
  if (typeof document === 'undefined') return
  const valid = clampScale(scale)
  const root = document.documentElement
  root.style.fontSize = `${(valid / 100) * 16}px`
  root.setAttribute('data-scale', String(valid))
}

/**
 * Хук управления масштабом интерфейса (UI Zoom)
 * Изменяет базовый font-size у html (на котором основаны все rem-единицы Tailwind),
 * обеспечивая гармоничное и адаптивное масштабирование без ломки вёрстки.
 */
export function useScale() {
  const [scale, setScaleState] = useState<number>(() => {
    if (typeof localStorage === 'undefined') return DEFAULT_SCALE
    const saved = localStorage.getItem(SCALE_STORAGE_KEY)
    if (saved) {
      const parsed = Number(saved)
      if (!Number.isNaN(parsed)) return clampScale(parsed)
    }
    return DEFAULT_SCALE
  })

  useEffect(() => {
    applyScale(scale)
  }, [scale])

  const setScale = useCallback((next: number) => {
    const clamped = clampScale(next)
    setScaleState(clamped)
    try {
      localStorage.setItem(SCALE_STORAGE_KEY, String(clamped))
    } catch {
      // Safari private mode fallback
    }
  }, [])

  const resetScale = useCallback(() => {
    setScale(DEFAULT_SCALE)
  }, [setScale])

  return {
    scale,
    setScale,
    resetScale,
    defaultScale: DEFAULT_SCALE,
    minScale: MIN_SCALE,
    maxScale: MAX_SCALE,
    step: SCALE_STEP,
  }
}
