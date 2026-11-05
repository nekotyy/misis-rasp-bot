import { describe, expect, it } from 'vitest'
import { MockAdapter } from '../adapters/mock'

describe('MockAdapter', () => {
  const adapter = new MockAdapter()

  it('платформа — mock', () => {
    expect(adapter.platform).toBe('mock')
  })

  it('getInitData() возвращает непустую строку', () => {
    const data = adapter.getInitData()
    expect(typeof data).toBe('string')
    expect(data.length).toBeGreaterThan(0)
  })

  it('getTheme() возвращает допустимое значение', () => {
    const theme = adapter.getTheme()
    expect(['light', 'dark', 'system']).toContain(theme)
  })

  it('getSafeAreaInsets() возвращает объект с числовыми полями', () => {
    const insets = adapter.getSafeAreaInsets()
    expect(typeof insets.top).toBe('number')
    expect(typeof insets.bottom).toBe('number')
    expect(typeof insets.left).toBe('number')
    expect(typeof insets.right).toBe('number')
  })

  it('getSafeAreaInsets() возвращает ненулевые safe area (мок)', () => {
    const insets = adapter.getSafeAreaInsets()
    expect(insets.top).toBeGreaterThanOrEqual(0)
    expect(insets.bottom).toBeGreaterThanOrEqual(0)
  })

  it('enableBackButton вызывается без ошибок', () => {
    expect(() => adapter.enableBackButton(() => {})).not.toThrow()
  })

  it('disableBackButton вызывается без ошибок', () => {
    expect(() => adapter.disableBackButton()).not.toThrow()
  })

  it('hapticFeedback вызывается без ошибок для всех типов', () => {
    expect(() => adapter.hapticFeedback('light')).not.toThrow()
    expect(() => adapter.hapticFeedback('medium')).not.toThrow()
    expect(() => adapter.hapticFeedback('heavy')).not.toThrow()
  })

  it('ready() вызывается без ошибок', () => {
    expect(() => adapter.ready()).not.toThrow()
  })
})
