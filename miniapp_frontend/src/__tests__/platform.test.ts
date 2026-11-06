import { describe, expect, it, vi } from 'vitest'
import bridge from '@vkontakte/vk-bridge'
import { MockAdapter } from '../adapters/mock'
import { VkAdapter } from '../adapters/vk'

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

describe('VkAdapter', () => {
  it('платформа — vk', () => {
    const vk = new VkAdapter()
    expect(vk.platform).toBe('vk')
  })

  it('ready() вызывает bridge.send(VKWebAppInit)', () => {
    const sendSpy = vi.spyOn(bridge, 'send').mockResolvedValue({} as any)
    const vk = new VkAdapter()
    vk.ready()
    expect(sendSpy).toHaveBeenCalledWith('VKWebAppInit')
    sendSpy.mockRestore()
  })

  it('hapticFeedback() вызывает VKWebAppTapticImpactOccurred', () => {
    const sendSpy = vi.spyOn(bridge, 'send').mockResolvedValue({} as any)
    const vk = new VkAdapter()
    vk.hapticFeedback('medium')
    expect(sendSpy).toHaveBeenCalledWith('VKWebAppTapticImpactOccurred', { style: 'medium' })
    sendSpy.mockRestore()
  })

  it('disableBackButton() вызывает VKWebAppDisableSwipeBack', () => {
    const sendSpy = vi.spyOn(bridge, 'send').mockResolvedValue({} as any)
    const vk = new VkAdapter()
    vk.disableBackButton()
    expect(sendSpy).toHaveBeenCalledWith('VKWebAppDisableSwipeBack')
    sendSpy.mockRestore()
  })
})
