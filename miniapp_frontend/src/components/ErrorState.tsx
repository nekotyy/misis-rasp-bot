import {
  AlertCircle,
  BookmarkX,
  RefreshCcw,
  ServerOff,
  ShieldAlert,
  WifiOff,
} from 'lucide-react'

type ErrorType = 'network' | 'server' | 'auth' | 'unknown' | 'no_subscription'

interface ErrorStateProps {
  type?: ErrorType
  message?: string
  onRetry?: () => void
}

const errorConfig: Record<ErrorType, { icon: typeof WifiOff; title: string; description: string }> = {
  network: {
    icon: WifiOff,
    title: 'Нет соединения',
    description: 'Проверьте интернет-соединение и попробуйте снова',
  },
  server: {
    icon: ServerOff,
    title: 'Ошибка сервера',
    description: 'Что-то пошло не так на нашей стороне. Попробуйте позже',
  },
  auth: {
    icon: ShieldAlert,
    title: 'Ошибка авторизации',
    description: 'Не удалось авторизоваться. Перезапустите приложение',
  },
  no_subscription: {
    icon: BookmarkX,
    title: 'Нет подписок',
    description: 'Добавьте группу в профиле, чтобы видеть расписание',
  },
  unknown: {
    icon: AlertCircle,
    title: 'Произошла ошибка',
    description: 'Попробуйте снова',
  },
}

export function ErrorState({ type = 'unknown', message, onRetry }: ErrorStateProps) {
  const config = errorConfig[type] || errorConfig.unknown
  const Icon = config.icon

  return (
    <div className="glass-card rounded-2xl p-8 m-4 flex flex-col items-center justify-center text-center gap-4">
      <div className="w-14 h-14 rounded-2xl bg-destructive-surface border border-destructive/25 flex items-center justify-center text-destructive shadow-lg shadow-destructive/10">
        <Icon className="w-7 h-7 stroke-[1.8]" />
      </div>
      <div className="space-y-1.5 max-w-xs">
        <h3 className="font-heading font-bold text-base text-foreground">{config.title}</h3>
        <p className="text-xs text-neutral leading-relaxed">
          {message ?? config.description}
        </p>
      </div>
      {onRetry && (
        <button
          onClick={onRetry}
          className="inline-flex items-center gap-2 px-5 py-2.5 rounded-xl bg-primary text-white font-heading font-semibold text-xs tracking-wide hover:bg-primary-hover shadow-lg shadow-primary/25 transition-all active:scale-95 cursor-pointer"
        >
          <RefreshCcw className="w-3.5 h-3.5" />
          <span>Повторить</span>
        </button>
      )}
    </div>
  )
}
