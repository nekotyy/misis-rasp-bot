import type { PlatformAdapter } from './platform'

/** Адаптер VK Mini Apps */
export class VkAdapter implements PlatformAdapter {
  readonly platform = 'vk' as const
  private launchParams: URLSearchParams

  constructor() {
    this.launchParams = new URLSearchParams(window.location.search)
  }

  getInitData(): string {
    return window.location.search.slice(1)
  }

  getTheme(): 'light' | 'dark' | 'system' {
    const scheme = this.launchParams.get('vk_app_id') ? 'system' : 'system'
    // VK передаёт vk_color_scheme в launch params
    const vkScheme = this.launchParams.get('vk_color_scheme')
    if (vkScheme === 'space_gray') return 'dark'
    if (vkScheme === 'bright_light') return 'light'
    return scheme
  }

  getSafeAreaInsets() {
    return { top: 0, bottom: 0, left: 0, right: 0 }
  }

  enableBackButton(callback: () => void): void {
    // VK использует событие go_back
    window.addEventListener('message', (e) => {
      if (e.data === 'go_back') callback()
    })
  }

  disableBackButton(): void {
    // VK не поддерживает programmatic disable — ничего не делаем
  }

  openLink(url: string): void {
    window.open(url, '_blank')
  }

  hapticFeedback(_type: 'light' | 'medium' | 'heavy'): void {
    // VK не имеет haptic API в Mini Apps
  }

  ready(): void {
    // VK SDK инициализирует автоматически
  }
}
