import type { PlatformAdapter } from './platform'

/** Мок-адаптер — только для разработки (VITE_MOCK_PLATFORM=true) */
export class MockAdapter implements PlatformAdapter {
  readonly platform = 'mock' as const

  getInitData(): string {
    return 'dev:10001:dev_student'
  }

  getTheme(): 'light' | 'dark' | 'system' {
    return 'system'
  }

  getSafeAreaInsets() {
    return { top: 12, bottom: 0, left: 0, right: 0 }
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
