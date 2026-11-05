import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { useTheme } from '../hooks/useTheme'

describe('useTheme', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
    document.documentElement.classList.remove('dark', 'light')
  })

  afterEach(() => {
    localStorage.clear()
  })

  it('по умолчанию возвращает system', () => {
    const { result } = renderHook(() => useTheme())
    expect(result.current.theme).toBe('system')
  })

  it('сохраняет тему в localStorage при изменении', () => {
    const { result } = renderHook(() => useTheme())
    act(() => {
      result.current.setTheme('dark')
    })
    expect(result.current.theme).toBe('dark')
    expect(localStorage.getItem('miniapp_theme')).toBe('dark')
  })

  it('устанавливает светлую тему', () => {
    const { result } = renderHook(() => useTheme())
    act(() => {
      result.current.setTheme('light')
    })
    expect(result.current.theme).toBe('light')
    expect(result.current.effectiveTheme).toBe('light')
  })

  it('устанавливает тёмную тему', () => {
    const { result } = renderHook(() => useTheme())
    act(() => {
      result.current.setTheme('dark')
    })
    expect(result.current.theme).toBe('dark')
    expect(result.current.effectiveTheme).toBe('dark')
  })

  it('переключает темы последовательно', () => {
    const { result } = renderHook(() => useTheme())
    act(() => result.current.setTheme('light'))
    expect(result.current.theme).toBe('light')
    act(() => result.current.setTheme('dark'))
    expect(result.current.theme).toBe('dark')
    act(() => result.current.setTheme('system'))
    expect(result.current.theme).toBe('system')
  })

  it('восстанавливает тему из localStorage', () => {
    localStorage.setItem('miniapp_theme', 'dark')
    const { result } = renderHook(() => useTheme())
    expect(result.current.theme).toBe('dark')
  })

  it('возвращает reducedMotion', () => {
    const { result } = renderHook(() => useTheme())
    expect(typeof result.current.reducedMotion).toBe('boolean')
  })
})
