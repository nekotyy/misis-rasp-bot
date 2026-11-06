import bridge from '@vkontakte/vk-bridge'
import type { PlatformAdapter } from './platform'

/** Адаптер VK Mini Apps с интеграцией VK Bridge */
export class VkAdapter implements PlatformAdapter {
  readonly platform = 'vk' as const
  private launchParams: URLSearchParams
  private rawInitData: string
  private safeArea = { top: 0, bottom: 0, left: 0, right: 0 }
  private currentTheme: 'light' | 'dark' | 'system' = 'system'
  private userPhoto?: string
  private userName?: { firstName?: string; lastName?: string }

  constructor() {
    this.rawInitData = this.extractRawInitData()
    this.launchParams = this.extractParams()
    this.initBridgeListeners()
  }

  private extractRawInitData(): string {
    if (typeof window === 'undefined') return ''
    if (window.location.search && (window.location.search.includes('vk_app_id') || window.location.search.includes('sign='))) {
      return window.location.search.replace(/^\?/, '')
    }
    if (window.location.hash && (window.location.hash.includes('vk_app_id') || window.location.hash.includes('sign='))) {
      return window.location.hash.replace(/^#/, '')
    }
    return window.location.search.replace(/^\?/, '')
  }

  private extractParams(): URLSearchParams {
    return new URLSearchParams(this.rawInitData)
  }

  private initBridgeListeners(): void {
    try {
      bridge.subscribe((e) => {
        if (!e || !e.detail) return
        const detail = e.detail as { type: string; data?: any }
        if (detail.type === 'VKWebAppUpdateConfig' && detail.data) {
          if (detail.data.scheme) {
            const s = String(detail.data.scheme).toLowerCase()
            if (s.includes('dark') || s === 'space_gray') {
              this.currentTheme = 'dark'
            } else if (s.includes('light') || s === 'bright_light') {
              this.currentTheme = 'light'
            }
          }
          if (detail.data.insets) {
            this.safeArea = {
              top: Number(detail.data.insets.top) || 0,
              bottom: Number(detail.data.insets.bottom) || 0,
              left: Number(detail.data.insets.left) || 0,
              right: Number(detail.data.insets.right) || 0,
            }
          }
        }
      })
    } catch {
      // Игнорируем в тестовых или автономных окружениях
    }
  }

  getInitData(): string {
    if (this.rawInitData) return this.rawInitData
    return this.extractRawInitData()
  }


  getTheme(): 'light' | 'dark' | 'system' {
    if (this.currentTheme !== 'system') return this.currentTheme
    const vkScheme = this.launchParams.get('vk_color_scheme')
    if (vkScheme === 'space_gray' || vkScheme?.includes('dark')) return 'dark'
    if (vkScheme === 'bright_light' || vkScheme?.includes('light')) return 'light'
    return 'system'
  }

  getSafeAreaInsets() {
    return this.safeArea
  }

  enableBackButton(callback: () => void): void {
    bridge.send('VKWebAppEnableSwipeBack').catch(() => {})
    try {
      bridge.subscribe((e) => {
        const type = (e.detail as { type?: string })?.type
        if (type === 'VKWebAppViewRestore' || type === 'VKWebAppGoBack') {
          callback()
        }
      })
    } catch {
      // Игнорируем
    }
  }

  disableBackButton(): void {
    bridge.send('VKWebAppDisableSwipeBack').catch(() => {})
  }

  openLink(url: string): void {
    if (typeof window !== 'undefined') {
      window.open(url, '_blank')
    }
  }

  hapticFeedback(type: 'light' | 'medium' | 'heavy'): void {
    const styleMap = {
      light: 'light' as const,
      medium: 'medium' as const,
      heavy: 'heavy' as const,
    }
    bridge.send('VKWebAppTapticImpactOccurred', { style: styleMap[type] || 'light' }).catch(() => {})
  }

  getUserPhoto(): string | undefined {
    return this.userPhoto
  }

  getUserName(): { firstName?: string; lastName?: string } | undefined {
    return this.userName
  }

  async fetchUserInfo(): Promise<{ photoUrl?: string; firstName?: string; lastName?: string } | null> {
    try {
      const data = await bridge.send('VKWebAppGetUserInfo')
      if (data) {
        this.userPhoto = data.photo_200 || data.photo_100 || data.photo_max_orig
        this.userName = { firstName: data.first_name, lastName: data.last_name }
        return {
          photoUrl: this.userPhoto,
          firstName: data.first_name,
          lastName: data.last_name,
        }
      }
    } catch {
      // Игнорируем в тестовом или автономном режиме
    }
    return null
  }

  ready(): void {
    bridge.send('VKWebAppInit').catch((err) => {
      console.warn('VKWebAppInit failed:', err)
    })
    this.fetchUserInfo().catch(() => {})
  }
}
