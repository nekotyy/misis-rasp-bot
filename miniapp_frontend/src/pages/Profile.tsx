import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Bell,
  BellOff,
  Check,
  Copy,
  Laptop,
  Link2,
  Moon,
  ShieldCheck,
  Star,
  Sun,
  Trash2,
  Unlink,
} from 'lucide-react'
import { useState } from 'react'
import {
  confirmLinkRequest,
  createLinkRequest,
  fetchProfile,
  unlinkIdentity,
  updatePreferences,
} from '../api/profile'
import { deleteSubscription, updateSubscription } from '../api/subscriptions'
import { ErrorState } from '../components/ErrorState'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { TelegramIcon, VkIcon } from '../components/PlatformIcons'
import type { Theme } from '../hooks/useTheme'
import { cn } from '../lib/utils'

interface ProfilePageProps {
  theme: Theme
  onThemeChange: (t: Theme) => void
  reducedMotion?: boolean
}

const themeOptions: { value: Theme; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { value: 'light', label: 'Светлая', icon: Sun },
  { value: 'dark', label: 'Тёмная', icon: Moon },
  { value: 'system', label: 'Системная', icon: Laptop },
]

export function ProfilePage({ theme, onThemeChange, reducedMotion = false }: ProfilePageProps) {
  const queryClient = useQueryClient()
  const [createdToken, setCreatedToken] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [inputToken, setInputToken] = useState('')
  const [linkError, setLinkError] = useState<string | null>(null)
  const [linkSuccess, setLinkSuccess] = useState<string | null>(null)
  const [showLinkSection, setShowLinkSection] = useState(false)

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

  const createTokenMutation = useMutation({
    mutationFn: createLinkRequest,
    onSuccess: (res) => {
      setCreatedToken(res.token)
      setLinkError(null)
    },
    onError: (err: Error) => {
      setLinkError(err.message || 'Ошибка создания кода связывания')
    },
  })

  const confirmLinkMutation = useMutation({
    mutationFn: (token: string) => confirmLinkRequest(token),
    onSuccess: () => {
      setLinkSuccess('Аккаунты успешно связаны!')
      setLinkError(null)
      setInputToken('')
      queryClient.invalidateQueries({ queryKey: ['profile'] })
    },
    onError: (err: Error) => {
      setLinkError(err.message || 'Не удалось привязать аккаунт')
    },
  })

  const unlinkMutation = useMutation({
    mutationFn: (platform: string) => unlinkIdentity(platform),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['profile'] })
    },
  })

  const handleCopyToken = () => {
    if (!createdToken) return
    navigator.clipboard.writeText(createdToken).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

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

  const notificationsActive =
    data?.preferences?.notifications_enabled ?? data?.preferences?.notify_enabled ?? true

  return (
    <div className="flex-1 flex flex-col min-h-0 overflow-y-auto">
      {/* Шапка профиля */}
      <div className="px-4 pt-4 pb-2">
        <h1 className="font-display font-black text-2xl tracking-tight text-foreground">
          Профиль
        </h1>
        <p className="text-sm text-neutral mt-1 leading-relaxed">
          Управление подписками, привязками аккаунтов и темой
        </p>
      </div>

      {isLoading ? (
        <div className="px-4 py-2">
          <LoadingSkeleton count={3} />
        </div>
      ) : data ? (
        <div className="px-4 pb-12 space-y-4">
          {/* Аккаунт и привязки */}
          <section className="glass-card rounded-2xl p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="font-heading text-xs font-bold text-neutral uppercase tracking-wider">
                Подключённые аккаунты
              </span>
              <span className="font-heading inline-flex items-center gap-1.5 text-xs text-primary font-bold">
                <ShieldCheck className="w-4 h-4" />
                Защищено
              </span>
            </div>

            {/* Список идентичностей */}
            <div className="space-y-2.5">
              {data.identities && data.identities.length > 0 ? (
                data.identities.map((ident) => {
                  const isCurrent =
                    data.current_identity.platform === ident.platform &&
                    data.current_identity.user_id === ident.user_id
                  const isTg = ident.platform === 'telegram'
                  const title =
                    ident.full_name ||
                    ident.username ||
                    ident.display_name ||
                    `ID: ${ident.user_id}`

                  return (
                    <div
                      key={`${ident.platform}-${ident.user_id}`}
                      className="flex items-center justify-between p-3.5 rounded-xl bg-card border border-border"
                    >
                      <div className="flex items-center gap-3.5 min-w-0">
                        <div
                          className="w-10 h-10 rounded-xl flex items-center justify-center flex-shrink-0 shadow-sm border border-border bg-primary-surface text-primary"
                        >
                          {isTg ? <TelegramIcon className="w-5 h-5" /> : <VkIcon className="w-5 h-5" />}
                        </div>
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <p className="font-heading text-base font-bold truncate text-foreground">{title}</p>
                            {isCurrent && (
                              <span className="font-heading text-xs font-bold uppercase tracking-wider px-2 py-0.5 rounded-full bg-primary-surface text-primary border border-primary/25">
                                Текущий
                              </span>
                            )}
                          </div>
                          <p className="text-sm text-neutral mt-0.5">
                            {isTg ? 'Telegram' : 'ВКонтакте'}
                          </p>
                        </div>
                      </div>

                      {data.identities.length > 1 && !isCurrent && (
                        <button
                          onClick={() => {
                            if (confirm(`Отвязать аккаунт ${isTg ? 'Telegram' : 'ВКонтакте'}?`)) {
                              unlinkMutation.mutate(ident.platform)
                            }
                          }}
                          className="p-2 rounded-lg text-neutral hover:text-destructive hover:bg-destructive-surface transition-colors cursor-pointer"
                          title="Отвязать аккаунт"
                        >
                          <Unlink className="w-4 h-4" />
                        </button>
                      )}
                    </div>
                  )
                })
              ) : (
                <div className="flex items-center gap-3.5 p-3.5 rounded-xl bg-card border border-border">
                  <div className="w-10 h-10 rounded-xl bg-primary-surface text-primary flex items-center justify-center border border-border">
                    {data.current_identity.platform === 'telegram' ? (
                      <TelegramIcon className="w-5 h-5 text-primary" />
                    ) : (
                      <VkIcon className="w-5 h-5 text-primary" />
                    )}
                  </div>
                  <div>
                    <p className="font-heading text-base font-bold text-foreground">
                      {data.current_identity.full_name ||
                        data.current_identity.username ||
                        data.current_identity.display_name ||
                        data.account_id}
                    </p>
                    <p className="text-sm text-neutral mt-0.5">
                      {data.current_identity.platform === 'telegram' ? 'Telegram' : 'ВКонтакте'}
                    </p>
                  </div>
                </div>
              )}
            </div>

            {/* Кнопка / блок связывания аккаунтов */}
            <div className="pt-1">
              {!showLinkSection ? (
                <button
                  onClick={() => setShowLinkSection(true)}
                  className="font-heading w-full flex items-center justify-center gap-2 py-3 px-4 rounded-xl border border-primary/30 bg-primary-surface text-primary text-sm font-bold hover:bg-primary hover:text-white transition-all cursor-pointer"
                >
                  <Link2 className="w-4 h-4" />
                  Связать с другим мессенджером
                </button>
              ) : (
                <div className="p-4 rounded-2xl bg-card border border-border space-y-3.5">
                  <div className="flex items-center justify-between">
                    <span className="font-heading text-sm font-bold text-foreground flex items-center gap-2">
                      <Link2 className="w-4 h-4 text-primary" />
                      Связывание аккаунтов
                    </span>
                    <button
                      onClick={() => setShowLinkSection(false)}
                      className="font-heading text-sm text-neutral hover:text-foreground cursor-pointer font-semibold"
                    >
                      Скрыть
                    </button>
                  </div>
                  <p className="text-sm text-neutral leading-relaxed">
                    Сгенерируйте код в одном клиенте и введите его в другом, чтобы синхронизировать ваши подписки и расписание.
                  </p>

                  {/* Генерация кода */}
                  <div className="space-y-3">
                    <button
                      type="button"
                      onClick={() => createTokenMutation.mutate()}
                      disabled={createTokenMutation.isPending}
                      className="font-heading w-full py-3 px-4 rounded-xl bg-primary hover:bg-primary-hover text-white text-sm font-bold shadow-md shadow-primary/25 active:scale-[0.98] transition-all disabled:opacity-50 cursor-pointer"
                    >
                      {createTokenMutation.isPending ? 'Генерация кода...' : 'Получить код связывания'}
                    </button>

                    {createdToken && (
                      <div className="p-3.5 rounded-xl bg-neutral-surface border border-primary/40 space-y-2.5 shadow-sm">
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-heading text-xs font-bold text-neutral uppercase tracking-wider">
                            Код связывания (5 мин)
                          </span>
                          <button
                            type="button"
                            onClick={handleCopyToken}
                            className={cn(
                              'font-heading flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-bold transition-all cursor-pointer flex-shrink-0',
                              copied
                                ? 'bg-success-surface text-success border border-success/40'
                                : 'bg-primary-surface text-primary border border-primary/30 hover:bg-primary hover:text-white active:scale-95',
                            )}
                          >
                            {copied ? (
                              <>
                                <Check className="w-4 h-4 text-success" />
                                <span>Скопировано</span>
                              </>
                            ) : (
                              <>
                                <Copy className="w-4 h-4" />
                                <span>Копировать</span>
                              </>
                            )}
                          </button>
                        </div>
                        <div className="p-3 rounded-lg bg-base border border-border select-all overflow-hidden">
                          <p className="font-mono font-bold text-sm text-primary tracking-wider break-all leading-relaxed select-all">
                            {createdToken}
                          </p>
                        </div>
                      </div>
                    )}
                  </div>

                  {/* Ввод кода от другого клиента */}
                  <div className="pt-2 border-t border-border space-y-2.5">
                    <label className="font-heading text-sm font-bold text-foreground block">
                      Или введите код, полученный в другом клиенте:
                    </label>
                    <div className="flex gap-2">
                      <input
                        type="text"
                        value={inputToken}
                        onChange={(e) => setInputToken(e.target.value.trim().toUpperCase())}
                        placeholder="Вставьте код..."
                        className="flex-1 min-w-0 px-3.5 py-2.5 rounded-xl bg-base border border-border text-sm font-mono uppercase tracking-wider text-foreground placeholder:text-neutral/50 focus:outline-none focus:ring-2 focus:ring-primary/50 focus:border-primary transition-all"
                      />
                      <button
                        type="button"
                        onClick={() => {
                          if (inputToken) confirmLinkMutation.mutate(inputToken)
                        }}
                        disabled={!inputToken || confirmLinkMutation.isPending}
                        className="font-heading px-4 py-2.5 rounded-xl bg-primary text-white text-sm font-bold hover:bg-primary-hover active:scale-95 transition-all disabled:opacity-40 cursor-pointer flex-shrink-0 shadow-md shadow-primary/25"
                      >
                        {confirmLinkMutation.isPending ? 'Проверка...' : 'Связать'}
                      </button>
                    </div>
                  </div>

                  {linkSuccess && (
                    <p className="font-heading text-sm text-success font-semibold">{linkSuccess}</p>
                  )}
                  {linkError && (
                    <p className="font-heading text-sm text-destructive font-semibold">{linkError}</p>
                  )}
                </div>
              )}
            </div>
          </section>

          {/* Подписки на группы */}
          <section className="glass-card rounded-2xl p-4 space-y-3">
            <div className="flex items-center justify-between">
              <span className="font-heading text-xs font-bold text-neutral uppercase tracking-wider">
                Мои группы
              </span>
              <span className="font-heading text-sm text-neutral font-bold">
                {data.subscriptions.length} {data.subscriptions.length === 1 ? 'группа' : 'групп'}
              </span>
            </div>

            {data.subscriptions.length === 0 ? (
              <div className="p-4 rounded-xl bg-card border border-border text-sm text-neutral text-center">
                Нет сохранённых групп.
              </div>
            ) : (
              <div className="space-y-2.5">
                {data.subscriptions.map((sub) => {
                  const notifEnabled = sub.notifications_enabled ?? sub.notify_enabled ?? true
                  return (
                    <div
                      key={sub.id}
                      className={cn(
                        'p-3.5 rounded-xl border flex items-center justify-between gap-3 transition-all',
                        sub.is_primary
                          ? 'bg-primary-surface/50 border-primary/40 shadow-sm'
                          : 'bg-card border-border',
                      )}
                    >
                      <div className="flex items-center gap-3 min-w-0">
                        {sub.is_primary ? (
                          <div className="w-8 h-8 rounded-lg bg-primary text-white flex items-center justify-center flex-shrink-0">
                            <Star className="w-4 h-4 fill-white" />
                          </div>
                        ) : (
                          <button
                            onClick={() => updateSubMutation.mutate({ id: sub.id, is_primary: true })}
                            title="Сделать основной группой"
                            className="w-8 h-8 rounded-lg bg-neutral-surface text-neutral hover:text-primary flex items-center justify-center flex-shrink-0 transition-colors cursor-pointer"
                          >
                            <Star className="w-4 h-4" />
                          </button>
                        )}
                        <div className="min-w-0">
                          <p className="font-heading font-bold text-base tracking-tight truncate text-foreground">
                            {sub.group_name || sub.subscription_title || 'Группа'}
                          </p>
                          {sub.is_primary && (
                            <p className="font-heading text-xs font-bold text-primary mt-0.5">
                              Основная группа
                            </p>
                          )}
                        </div>
                      </div>

                      <div className="flex items-center gap-1.5 flex-shrink-0">
                        {/* Кнопка уведомлений по группе */}
                        <button
                          onClick={() =>
                            toggleNotifyMutation.mutate({ id: sub.id, enabled: !notifEnabled })
                          }
                          title={notifEnabled ? 'Отключить уведомления группы' : 'Включить уведомления группы'}
                          className={cn(
                            'p-2.5 rounded-lg transition-colors cursor-pointer',
                            notifEnabled
                              ? 'text-primary bg-primary-surface hover:bg-primary hover:text-white'
                              : 'text-neutral bg-neutral-surface hover:text-foreground',
                          )}
                        >
                          {notifEnabled ? <Bell className="w-4 h-4" /> : <BellOff className="w-4 h-4" />}
                        </button>

                        {/* Удалить подписку */}
                        <button
                          onClick={() => {
                            if (confirm(`Удалить подписку на ${sub.group_name || 'группу'}?`)) {
                              deleteSubMutation.mutate(sub.id)
                            }
                          }}
                          title="Удалить подписку"
                          className="p-2.5 rounded-lg text-neutral hover:text-destructive hover:bg-destructive-surface transition-colors cursor-pointer"
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </div>
                  )
                })}
              </div>
            )}
          </section>

          {/* Настройки уведомлений */}
          <section className="glass-card rounded-2xl p-4 space-y-3">
            <span className="font-heading text-xs font-bold text-neutral uppercase tracking-wider block">
              Уведомления
            </span>
            <div className="flex items-center justify-between gap-3">
              <div className="space-y-1">
                <p className="font-heading text-base font-bold text-foreground">
                  Оповещения об изменениях
                </p>
                <p className="text-sm text-neutral leading-relaxed">
                  Мгновенные сообщения при замене аудиторий или пар
                </p>
              </div>
              <button
                type="button"
                onClick={() => updatePrefsMutation.mutate(!notificationsActive)}
                className={cn(
                  'w-13 h-7.5 rounded-full p-0.5 relative cursor-pointer border transition-all duration-200 focus:outline-none focus:ring-2 focus:ring-primary/40 flex-shrink-0',
                  notificationsActive
                    ? 'bg-primary border-primary shadow-[0_2px_12px_rgba(79,133,235,0.45)]'
                    : 'bg-neutral-surface border-neutral/40 hover:border-neutral',
                )}
                role="switch"
                aria-checked={notificationsActive}
              >
                <span
                  className={cn(
                    'block w-6 h-6 rounded-full shadow-md transform mt-0.5 ml-0.5 transition-transform duration-200',
                    notificationsActive ? 'bg-white translate-x-5.5' : 'bg-neutral translate-x-0',
                    reducedMotion && 'transition-none',
                  )}
                />
              </button>
            </div>
          </section>

          {/* Оформление (Тема) */}
          <section className="glass-card rounded-2xl p-4 space-y-3">
            <span className="font-heading text-xs font-bold text-neutral uppercase tracking-wider block">
              Тема оформления
            </span>
            <div className="grid grid-cols-3 gap-2.5">
              {themeOptions.map((opt) => {
                const Icon = opt.icon
                const isActive = theme === opt.value
                return (
                  <button
                    key={opt.value}
                    type="button"
                    onClick={() => onThemeChange(opt.value)}
                    className={cn(
                      'flex flex-col items-center justify-center gap-2 py-3.5 px-2 rounded-xl border transition-all duration-200 cursor-pointer',
                      isActive
                        ? 'bg-primary text-white border-primary shadow-[0_4px_16px_rgba(79,133,235,0.45)] ring-2 ring-primary/40 font-bold scale-[1.02]'
                        : 'bg-card border-border text-neutral hover:bg-neutral-hover/10 hover:text-foreground',
                    )}
                  >
                    <Icon className={cn('w-5 h-5', isActive ? 'text-white' : 'text-neutral')} />
                    <span className={cn('font-heading text-xs', isActive ? 'text-white font-bold' : 'font-semibold')}>{opt.label}</span>
                  </button>
                )
              })}
            </div>
          </section>
        </div>
      ) : null}
    </div>
  )
}
