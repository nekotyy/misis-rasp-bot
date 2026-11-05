import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bell, BellOff, LogOut, Moon, Star, Sun, Trash2 } from 'lucide-react'
import { fetchProfile, updatePreferences } from '../api/profile'
import { deleteSubscription, updateSubscription } from '../api/subscriptions'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import type { Theme } from '../hooks/useTheme'
import { cn } from '../lib/utils'

interface ProfilePageProps {
  theme: Theme
  onThemeChange: (t: Theme) => void
  reducedMotion?: boolean
}

const themeOptions: { value: Theme; label: string; icon: React.ReactNode }[] = [
  { value: 'light', label: 'Светлая', icon: <Sun className="w-4 h-4" /> },
  { value: 'dark', label: 'Тёмная', icon: <Moon className="w-4 h-4" /> },
  { value: 'system', label: 'Системная', icon: <span className="text-sm">🖥</span> },
]

function platformLabel(platform: string): string {
  switch (platform) {
    case 'telegram':
      return '🤖 Telegram'
    case 'vk':
      return '💙 ВКонтакте'
    default:
      return platform
  }
}

export function ProfilePage({ theme, onThemeChange, reducedMotion = false }: ProfilePageProps) {
  const queryClient = useQueryClient()

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['profile'],
    queryFn: fetchProfile,
    staleTime: 60 * 1000,
  })

  const updateSubMutation = useMutation({
    mutationFn: ({ id, is_primary }: { id: number; is_primary: boolean }) =>
      updateSubscription(id, { is_primary }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['profile'] }),
  })

  const toggleNotifyMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) =>
      updateSubscription(id, { notify_enabled: enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['profile'] }),
  })

  const deleteSubMutation = useMutation({
    mutationFn: (id: number) => deleteSubscription(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['profile'] }),
  })

  const updatePrefsMutation = useMutation({
    mutationFn: (enabled: boolean) => updatePreferences({ notify_enabled: enabled }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['profile'] }),
  })

  if (error) {
    return (
      <div className="flex-1 flex flex-col">
        <ErrorState
          type="server"
          message={(error as Error).message}
          onRetry={() => refetch()}
        />
      </div>
    )
  }

  return (
    <div className="flex-1 flex flex-col min-h-0 overflow-y-auto">
      <div className="px-4 pt-3 pb-2">
        <h1 className="text-lg font-bold">Профиль</h1>
      </div>

      {isLoading ? (
        <div className="px-4">
          <LoadingSkeleton count={3} />
        </div>
      ) : data ? (
        <div className="px-4 pb-6 space-y-5">
          {/* Информация об аккаунте */}
          <section className="bg-card border border-border rounded-xl p-4 space-y-2">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
              Аккаунт
            </h2>
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-full bg-primary/10 flex items-center justify-center text-xl">
                {data.current_identity.platform === 'telegram' ? '🤖' : '💙'}
              </div>
              <div>
                <p className="font-medium text-sm">
                  {data.current_identity.full_name ??
                    data.current_identity.username ??
                    data.current_identity.display_name ??
                    data.account_id}
                </p>
                <p className="text-xs text-muted-foreground">
                  {platformLabel(data.current_identity.platform)}
                </p>
              </div>
            </div>
          </section>

          {/* Подписки */}
          <section className="space-y-2">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide px-1">
              Группы
            </h2>
            {data.subscriptions.length === 0 ? (
              <div className="bg-card border border-border rounded-xl p-4 text-sm text-muted-foreground text-center">
                Нет подписок. Добавьте группу ниже.
              </div>
            ) : (
              <div className="space-y-2">
                {data.subscriptions.map((sub) => (
                  <div
                    key={sub.id}
                    className="bg-card border border-border rounded-xl p-3 flex items-center justify-between gap-2"
                  >
                    <div className="flex items-center gap-2 min-w-0">
                      {sub.is_primary && (
                        <Star className="w-4 h-4 text-amber-500 flex-shrink-0" fill="currentColor" />
                      )}
                      <span className="font-medium text-sm truncate">{sub.group_name}</span>
                    </div>
                    <div className="flex items-center gap-1 flex-shrink-0">
                      {!sub.is_primary && (
                        <button
                          onClick={() =>
                            updateSubMutation.mutate({ id: sub.id, is_primary: true })
                          }
                          title="Сделать основной"
                          className="p-1.5 rounded-lg hover:bg-muted transition-colors"
                        >
                          <Star className="w-4 h-4 text-muted-foreground" />
                        </button>
                      )}
                      <button
                        onClick={() => {
                          const active = sub.notifications_enabled ?? sub.notify_enabled ?? true
                          toggleNotifyMutation.mutate({ id: sub.id, enabled: !active })
                        }}
                        title={
                          (sub.notifications_enabled ?? sub.notify_enabled ?? true)
                            ? 'Отключить уведомления'
                            : 'Включить уведомления'
                        }
                        className={cn(
                          'p-1.5 rounded-lg transition-colors',
                          (sub.notifications_enabled ?? sub.notify_enabled ?? true)
                            ? 'text-primary hover:bg-primary/10'
                            : 'text-muted-foreground hover:bg-muted',
                        )}
                      >
                        {(sub.notifications_enabled ?? sub.notify_enabled ?? true) ? (
                          <Bell className="w-4 h-4" />
                        ) : (
                          <BellOff className="w-4 h-4" />
                        )}
                      </button>
                      <button
                        onClick={() => {
                          if (confirm(`Удалить подписку на ${sub.group_name}?`)) {
                            deleteSubMutation.mutate(sub.id)
                          }
                        }}
                        title="Отписаться"
                        className="p-1.5 rounded-lg hover:bg-destructive/10 text-destructive transition-colors"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Уведомления */}
          <section className="bg-card border border-border rounded-xl p-4 space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
              Уведомления
            </h2>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Bell className="w-4 h-4 text-muted-foreground" />
                <span className="text-sm">Уведомления об изменениях</span>
              </div>
              <button
                onClick={() => {
                  const active = data.preferences.notifications_enabled ?? data.preferences.notify_enabled ?? true
                  updatePrefsMutation.mutate(!active)
                }}
                className={cn(
                  'w-10 h-6 rounded-full transition-colors relative',
                  reducedMotion ? '' : 'transition-colors',
                  (data.preferences.notifications_enabled ?? data.preferences.notify_enabled ?? true) ? 'bg-primary' : 'bg-muted',
                )}
                role="switch"
                aria-checked={data.preferences.notifications_enabled ?? data.preferences.notify_enabled ?? true}
              >
                <span
                  className={cn(
                    'absolute top-0.5 w-5 h-5 rounded-full bg-white shadow-sm',
                    reducedMotion ? '' : 'transition-transform',
                    (data.preferences.notifications_enabled ?? data.preferences.notify_enabled ?? true) ? 'translate-x-4' : 'translate-x-0.5',
                  )}
                />
              </button>
            </div>
          </section>

          {/* Тема */}
          <section className="bg-card border border-border rounded-xl p-4 space-y-3">
            <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wide">
              Тема оформления
            </h2>
            <div className="grid grid-cols-3 gap-2">
              {themeOptions.map((opt) => (
                <button
                  key={opt.value}
                  onClick={() => onThemeChange(opt.value)}
                  className={cn(
                    'flex flex-col items-center gap-1 py-2 rounded-lg border text-xs font-medium',
                    reducedMotion ? '' : 'transition-colors',
                    theme === opt.value
                      ? 'bg-primary text-primary-foreground border-primary'
                      : 'border-border hover:bg-muted text-muted-foreground',
                  )}
                >
                  {opt.icon}
                  <span>{opt.label}</span>
                </button>
              ))}
            </div>
          </section>

          {/* Выход */}
          <button className="w-full flex items-center justify-center gap-2 py-3 px-4 rounded-xl border border-destructive/30 text-destructive text-sm font-medium hover:bg-destructive/5 transition-colors">
            <LogOut className="w-4 h-4" />
            Выйти
          </button>
        </div>
      ) : null}
    </div>
  )
}
