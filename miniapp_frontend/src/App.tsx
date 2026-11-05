import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Navigate, Route, Routes } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { fetchProfile } from './api/profile'
import { BottomNav } from './components/BottomNav'
import { ErrorState } from './components/ErrorState'
import { usePlatform } from './hooks/usePlatform'
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
  authState: 'idle' | 'loading' | 'authenticated' | 'error'
  error?: string | null
  onRetry?: () => void
}

function AppShell({ authState, error, onRetry }: AppShellProps) {
  const adapter = usePlatform()
  const { theme, setTheme, reducedMotion } = useTheme(adapter)
  const safeAreaInsets = adapter.getSafeAreaInsets()

  const { data: profile } = useQuery({
    queryKey: ['profile'],
    queryFn: fetchProfile,
    enabled: authState === 'authenticated',
    staleTime: 60 * 1000,
  })

  const primarySubscription = profile?.subscriptions.find((s) => s.is_primary)
  const subscriptionId = primarySubscription?.id
  const subscriptionTitle = primarySubscription?.group_name ?? undefined

  if (authState === 'loading' || authState === 'idle') {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="text-center space-y-3">
          <div className="w-12 h-12 mx-auto rounded-full bg-primary/10 flex items-center justify-center">
            <span className="text-2xl">📅</span>
          </div>
          <p className="text-sm text-muted-foreground">Загрузка...</p>
        </div>
      </div>
    )
  }

  if (authState === 'error') {
    return (
      <div className="flex h-screen items-center justify-center px-6">
        <ErrorState type="auth" message={error ?? undefined} onRetry={onRetry} />
      </div>
    )
  }

  return (
    <div
      className="flex flex-col h-screen bg-background text-foreground"
      style={{
        paddingTop: `max(${safeAreaInsets.top}px, env(safe-area-inset-top, 0px))`,
      }}
    >
      {/* Контент — занимает всё место кроме нижней навигации */}
      <div
        className="flex-1 flex flex-col min-h-0 overflow-hidden"
        style={{
          paddingBottom: `calc(60px + max(${safeAreaInsets.bottom}px, env(safe-area-inset-bottom, 0px)))`,
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
                reducedMotion={reducedMotion}
              />
            }
          />
          <Route
            path="/history"
            element={
              <HistoryPage
                subscriptionId={subscriptionId}
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

interface AppProps {
  authState: 'idle' | 'loading' | 'authenticated' | 'error'
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
