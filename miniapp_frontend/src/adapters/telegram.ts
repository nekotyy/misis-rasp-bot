import type { PlatformAdapter } from './platform'

/** Адаптер Telegram Mini App (window.Telegram.WebApp) */
export class TelegramAdapter implements PlatformAdapter {
  readonly platform = 'telegram' as const
  private tg = (window as typeof window & { Telegram?: { WebApp?: Record<string, unknown> } }).Telegram?.WebApp as
    | Record<string, unknown>
    | undefined

  getInitData(): string {
    return (this.tg?.initData as string | undefined) ?? ''
  }

  getTheme(): 'light' | 'dark' | 'system' {
    const colorScheme = this.tg?.colorScheme as string | undefined
    if (colorScheme === 'dark') return 'dark'
    if (colorScheme === 'light') return 'light'
    return 'system'
  }

  getSafeAreaInsets() {
    const safeArea = this.tg?.safeAreaInset as Record<string, number> | undefined
    return {
      top: safeArea?.top ?? 0,
      bottom: safeArea?.bottom ?? 0,
      left: safeArea?.left ?? 0,
      right: safeArea?.right ?? 0,
    }
  }

  enableBackButton(callback: () => void): void {
    const backButton = this.tg?.BackButton as
      | { show: () => void; onClick: (cb: () => void) => void }
      | undefined
    if (backButton) {
      backButton.onClick(callback)
      backButton.show()
    }
  }

  disableBackButton(): void {
    const backButton = this.tg?.BackButton as
      | { hide: () => void; offClick: (cb?: () => void) => void }
      | undefined
    if (backButton) {
      backButton.offClick()
      backButton.hide()
    }
  }

  openLink(url: string): void {
    const tg = this.tg as { openLink?: (url: string) => void } | undefined
    if (tg?.openLink) {
      tg.openLink(url)
    } else {
      window.open(url, '_blank')
    }
  }

  hapticFeedback(type: 'light' | 'medium' | 'heavy'): void {
    const hf = (this.tg as { HapticFeedback?: { impactOccurred: (s: string) => void } } | undefined)
      ?.HapticFeedback
    hf?.impactOccurred(type)
  }

  ready(): void {
    const tg = this.tg as { ready?: () => void } | undefined
    tg?.ready?.()
  }
}
