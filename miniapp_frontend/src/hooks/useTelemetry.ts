import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import { sendTelemetryPing, sendTelemetrySession } from '../api/telemetry'
import { usePlatform } from './usePlatform'

function getOrCreateSessionId(): string {
  try {
    let sid = sessionStorage.getItem('misis_miniapp_sid')
    if (!sid) {
      sid = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
      sessionStorage.setItem('misis_miniapp_sid', sid)
    }
    return sid
  } catch {
    return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  }
}

interface UseTelemetryOptions {
  activeGroup?: string
  theme?: string
  scale?: number
  enabled?: boolean
}

export function useTelemetry({
  activeGroup,
  theme = 'system',
  scale = 100,
  enabled = true,
}: UseTelemetryOptions = {}) {
  const platform = usePlatform()
  const location = useLocation()
  const startTimeRef = useRef<number>(Date.now())
  const sessionIdRef = useRef<string>(getOrCreateSessionId())
  const lastPageRef = useRef<string>(location.pathname)
  const activeGroupRef = useRef<string | undefined>(activeGroup)

  activeGroupRef.current = activeGroup
  lastPageRef.current = location.pathname

  // 1. Инициализация сессии на старте
  useEffect(() => {
    if (!enabled) return

    const sid = sessionIdRef.current
    const screenWidth = typeof window !== 'undefined' ? window.innerWidth : 0
    const screenHeight = typeof window !== 'undefined' ? window.innerHeight : 0
    const userAgent = typeof navigator !== 'undefined' ? navigator.userAgent : ''

    sendTelemetrySession({
      session_id: sid,
      platform: platform.platform || 'telegram',
      user_agent: userAgent,
      screen_width: screenWidth,
      screen_height: screenHeight,
      theme,
      scale,
      active_group: activeGroup,
      last_page: location.pathname,
    })
  }, [enabled, platform.platform, scale, theme])

  // 2. Периодический пинг активности (каждые 20 сек)
  useEffect(() => {
    if (!enabled) return

    const sendPing = () => {
      const durationSeconds = Math.max(1, Math.round((Date.now() - startTimeRef.current) / 1000))
      sendTelemetryPing({
        session_id: sessionIdRef.current,
        duration_seconds: durationSeconds,
        active_group: activeGroupRef.current,
        current_page: lastPageRef.current,
      })
    }

    const interval = setInterval(sendPing, 20000)

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'hidden') {
        sendPing()
      }
    }

    window.addEventListener('visibilitychange', handleVisibilityChange)
    window.addEventListener('beforeunload', sendPing)

    return () => {
      clearInterval(interval)
      window.removeEventListener('visibilitychange', handleVisibilityChange)
      window.removeEventListener('beforeunload', sendPing)
      sendPing()
    }
  }, [enabled])

  // 3. Отправка пинга при смене страницы
  useEffect(() => {
    if (!enabled) return
    const durationSeconds = Math.max(1, Math.round((Date.now() - startTimeRef.current) / 1000))
    sendTelemetryPing({
      session_id: sessionIdRef.current,
      duration_seconds: durationSeconds,
      active_group: activeGroupRef.current,
      current_page: location.pathname,
    })
  }, [location.pathname, enabled])
}
