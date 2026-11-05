import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  clampScale,
  DEFAULT_SCALE,
  MAX_SCALE,
  MIN_SCALE,
  SCALE_STORAGE_KEY,
  useScale,
} from '../hooks/useScale'

describe('useScale hook', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('data-scale')
    document.documentElement.style.fontSize = ''
  })

  afterEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('data-scale')
    document.documentElement.style.fontSize = ''
  })

  it('инициализируется с дефолтным масштабом 100%', () => {
    const { result } = renderHook(() => useScale())
    expect(result.current.scale).toBe(100)
    expect(result.current.defaultScale).toBe(DEFAULT_SCALE)
    expect(document.documentElement.style.fontSize).toBe('16px')
    expect(document.documentElement.getAttribute('data-scale')).toBe('100')
  })

  it('сохраняет изменённый масштаб в localStorage и на root элемент', () => {
    const { result } = renderHook(() => useScale())
    act(() => {
      result.current.setScale(125)
    })
    expect(result.current.scale).toBe(125)
    expect(localStorage.getItem(SCALE_STORAGE_KEY)).toBe('125')
    expect(document.documentElement.style.fontSize).toBe('20px')
    expect(document.documentElement.getAttribute('data-scale')).toBe('125')
  })

  it('ограничивает масштаб в допустимом диапазоне (clampScale)', () => {
    expect(clampScale(50)).toBe(MIN_SCALE)
    expect(clampScale(200)).toBe(MAX_SCALE)
    expect(clampScale(105)).toBe(105)
    expect(clampScale(NaN)).toBe(DEFAULT_SCALE)
  })

  it('не позволяет установить значение меньше MIN_SCALE или больше MAX_SCALE', () => {
    const { result } = renderHook(() => useScale())
    act(() => {
      result.current.setScale(50)
    })
    expect(result.current.scale).toBe(MIN_SCALE)

    act(() => {
      result.current.setScale(200)
    })
    expect(result.current.scale).toBe(MAX_SCALE)
  })

  it('восстанавливает сохранённый масштаб из localStorage при загрузке', () => {
    localStorage.setItem(SCALE_STORAGE_KEY, '120')
    const { result } = renderHook(() => useScale())
    expect(result.current.scale).toBe(120)
    expect(document.documentElement.style.fontSize).toBe('19.2px')
    expect(document.documentElement.getAttribute('data-scale')).toBe('120')
  })

  it('корректно сбрасывает масштаб на дефолтный через resetScale', () => {
    const { result } = renderHook(() => useScale())
    act(() => {
      result.current.setScale(130)
    })
    expect(result.current.scale).toBe(130)

    act(() => {
      result.current.resetScale()
    })
    expect(result.current.scale).toBe(DEFAULT_SCALE)
    expect(localStorage.getItem(SCALE_STORAGE_KEY)).toBe(String(DEFAULT_SCALE))
  })
})
