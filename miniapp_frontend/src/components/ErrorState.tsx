import { AlertCircle, RefreshCcw, ServerOff, WifiOff } from 'lucide-react'

type ErrorType = 'network' | 'server' | 'auth' | 'unknown' | 'no_subscription'

interface ErrorStateProps {
  type?: ErrorType
  message?: string
  onRetry?: () => void
}

const errorConfig: Record<ErrorType, { icon: React.ReactNode; title: string; description: string }> = {
  network: {
    icon: <WifiOff className="w-10 h-10" />,
    title: 'Нет соединения',
    description: 'Проверьте интернет-соединение и попробуйте снова',
  },
  server: {
    icon: <ServerOff className="w-10 h-10" />,
    title: 'Ошибка сервера',
    description: 'Что-то пошло не так на нашей стороне. Попробуйте позже',
  },
  auth: {
    icon: <AlertCircle className="w-10 h-10" />,
    title: 'Ошибка авторизации',
    description: 'Не удалось авторизоваться. Перезапустите приложение',
  },
  no_subscription: {
    icon: <span className="text-4xl">📋</span>,
    title: 'Нет подписок',
    description: 'Добавьте группу в профиле, чтобы видеть расписание',
  },
  unknown: {
    icon: <AlertCircle className="w-10 h-10" />,
    title: 'Произошла ошибка',
    description: 'Попробуйте снова',
  },
}

export function ErrorState({ type = 'unknown', message, onRetry }: ErrorStateProps) {
  const config = errorConfig[type]

  return (
    <div className="flex flex-col items-center justify-center py-12 px-6 gap-4 text-muted-foreground">
      <div className="opacity-40">{config.icon}</div>
      <div className="text-center space-y-1">
        <p className="text-base font-semibold text-foreground">{config.title}</p>
        <p className="text-sm">{message ?? config.description}</p>
      </div>
      {onRetry && (
        <button
          onClick={onRetry}
          className="flex items-center gap-2 px-4 py-2 rounded-lg bg-primary text-primary-foreground text-sm font-medium hover:bg-primary/90 transition-colors"
        >
          <RefreshCcw className="w-4 h-4" />
          Повторить
        </button>
      )}
    </div>
  )
}
