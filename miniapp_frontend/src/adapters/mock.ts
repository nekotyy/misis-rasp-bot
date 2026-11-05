import type { PlatformAdapter } from './platform'

/** Мок-адаптер — только для разработки (VITE_MOCK_PLATFORM=true) */
export class MockAdapter implements PlatformAdapter {
  readonly platform = 'mock' as const

  getInitData(): string {
    return 'mock_init_data=test&user=%7B%22id%22%3A123456%2C%22first_name%22%3A%22Test%22%7D&auth_date=1234567890&hash=mock_hash'
  }

  getTheme(): 'light' | 'dark' | 'system' {
    return 'system'
  }

  getSafeAreaInsets() {
    return { top: 44, bottom: 34, left: 0, right: 0 }
  }

  enableBackButton(callback: () => void): void {
    console.debug('[MockAdapter] enableBackButton registered', callback)
  }

  disableBackButton(): void {
    console.debug('[MockAdapter] disableBackButton')
  }

  openLink(url: string): void {
    console.debug('[MockAdapter] openLink', url)
    window.open(url, '_blank')
  }

  hapticFeedback(type: 'light' | 'medium' | 'heavy'): void {
    console.debug('[MockAdapter] hapticFeedback', type)
  }

  ready(): void {
    console.debug('[MockAdapter] ready()')
  }
}
