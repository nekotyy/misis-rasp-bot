/** Единый интерфейс платформенного адаптера */
export interface PlatformAdapter {
  platform: 'telegram' | 'vk' | 'mock'
  getInitData(): string
  getTheme(): 'light' | 'dark' | 'system'
  getSafeAreaInsets(): { top: number; bottom: number; left: number; right: number }
  enableBackButton(callback: () => void): void
  disableBackButton(): void
  openLink(url: string): void
  hapticFeedback(type: 'light' | 'medium' | 'heavy'): void
  ready(): void
}
