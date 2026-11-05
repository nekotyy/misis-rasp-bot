import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Loader2 } from 'lucide-react'
import { useState } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { fetchProfile } from './api/profile'
import { BottomNav } from './components/BottomNav'
import { BrowserBlockScreen } from './components/BrowserBlockScreen'
import { ErrorState } from './components/ErrorState'
import type { AuthState } from './hooks/useAuth'
import { usePlatform } from './hooks/usePlatform'
import { useScale } from './hooks/useScale'
import { useTheme } from './hooks/useTheme'
import { HistoryPage } from './pages/History'
import { ProfilePage } from './pages/Profile'
import { SchedulePage } from './pages/Schedule'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 2,
      refetchOnWindowFocus: false,
    },
  },
})

interface AppShellProps {
  authState: AuthState
  error?: string | null
  onRetry?: () => void
}

function AppShell({ authState, error, onRetry }: AppShellProps) {
  const adapter = usePlatform()
  const { theme, setTheme, reducedMotion } = useTheme(adapter)
  const { scale, setScale } = useScale()
  const safeAreaInsets = adapter.getSafeAreaInsets()

  const [selectedSubId, setSelectedSubId] = useState<number | null>(null)

  const { data: profile } = useQuery({
    queryKey: ['profile'],
    queryFn: fetchProfile,
    enabled: authState === 'authenticated',
    staleTime: 60 * 1000,
  })

  const subscriptions = profile?.subscriptions ?? []
  const activeSub =
    subscriptions.find((s) => s.id === selectedSubId) ||
    subscriptions.find((s) => s.is_primary) ||
    subscriptions[0]
  const subscriptionId = activeSub?.id
  const subscriptionTitle = activeSub?.group_name || activeSub?.subscription_title || undefined

  if (authState === 'loading' || authState === 'idle') {
    return (
      <div className="flex h-screen items-center justify-center relative overflow-hidden bg-base">
        <div className="ambient-glow" aria-hidden>
          <div className="ambient-orb-1" />
          <div className="ambient-orb-2" />
        </div>
        <div className="glass-card rounded-3xl p-8 text-center space-y-4 max-w-xs mx-4 z-10 shadow-2xl">
          <div className="w-14 h-14 mx-auto rounded-2xl bg-primary-surface border border-primary/25 flex items-center justify-center text-primary shadow-lg shadow-primary/20">
            <Loader2 className="w-7 h-7 animate-spin stroke-[2]" />
          </div>
          <div>
            <h2 className="font-heading font-bold text-base text-foreground">Загрузка данных</h2>
            <p className="text-xs text-neutral mt-1">Подключаемся к сервису расписания...</p>
          </div>
        </div>
      </div>
    )
  }

  if (authState === 'unsupported_browser') {
    return <BrowserBlockScreen message={error ?? undefined} />
  }

  if (authState === 'error') {
    return (
      <div className="flex h-screen items-center justify-center px-6 relative bg-[var(--bg-base)]">
        <div className="ambient-glow" aria-hidden>
          <div className="ambient-orb-1" />
          <div className="ambient-orb-2" />
        </div>
        <div className="z-10 w-full max-w-sm">
          <ErrorState type="auth" message={error ?? undefined} onRetry={onRetry} />
        </div>
      </div>
    )
  }

  return (
    <div
      className="flex flex-col h-screen relative overflow-hidden text-foreground"
      style={{
        background: 'var(--bg-base)',
        paddingTop: `max(${safeAreaInsets.top}px, env(safe-area-inset-top, 0px))`,
      }}
    >
      {/* Декоративное жидкое свечение заднего плана */}
      <div className="ambient-glow" aria-hidden>
        <div className="ambient-orb-1" />
        <div className="ambient-orb-2" />
      </div>

      {/* Контент страниц */}
      <div
        className="flex-1 flex flex-col min-h-0 overflow-hidden z-10"
        style={{
          paddingBottom: `calc(68px + max(${safeAreaBottom(safeAreaInsets.bottom)}px, env(safe-area-inset-bottom, 0px)))`,
        }}
      >
        <Routes>
          <Route index element={<Navigate to="/schedule" replace />} />
          <Route
            path="/schedule"
            element={
              <SchedulePage
                subscriptionId={subscriptionId}
                subscriptionTitle={subscriptionTitle}
                subscriptions={subscriptions}
                onSelectSubscription={setSelectedSubId}
                reducedMotion={reducedMotion}
              />
            }
          />
          <Route
            path="/history"
            element={
              <HistoryPage
                subscriptionId={subscriptionId}
                subscriptionTitle={subscriptionTitle}
                subscriptions={subscriptions}
                onSelectSubscription={setSelectedSubId}
                reducedMotion={reducedMotion}
              />
            }
          />
          <Route
            path="/profile"
            element={
              <ProfilePage
                theme={theme}
                onThemeChange={setTheme}
                scale={scale}
                onScaleChange={setScale}
                reducedMotion={reducedMotion}
              />
            }
          />
          <Route path="*" element={<Navigate to="/schedule" replace />} />
        </Routes>
      </div>

      {/* Нижняя навигация */}
      <BottomNav safeAreaBottom={safeAreaInsets.bottom} />
    </div>
  )
}

function safeAreaBottom(bottom: number): number {
  return bottom > 0 ? bottom : 12
}

export interface AppProps {
  authState: AuthState
  error?: string | null
  onRetry?: () => void
}

export function App({ authState, error, onRetry }: AppProps) {
  return (
    <QueryClientProvider client={queryClient}>
      <AppShell authState={authState} error={error} onRetry={onRetry} />
    </QueryClientProvider>
  )
}
