import { ArrowUpRight, Lock, ShieldAlert, Smartphone } from 'lucide-react'
import { TelegramIcon, VkIcon } from './PlatformIcons'

interface BrowserBlockScreenProps {
  message?: string
  telegramUrl?: string
  vkUrl?: string
}

const DEFAULT_TG_URL =
  import.meta.env.VITE_TELEGRAM_BOT_URL || 'https://t.me/misis_rasp_bot'
const DEFAULT_VK_URL =
  import.meta.env.VITE_VK_BOT_URL || 'https://vk.com/misis_rasp_bot'

export function BrowserBlockScreen({
  message,
  telegramUrl = DEFAULT_TG_URL,
  vkUrl = DEFAULT_VK_URL,
}: BrowserBlockScreenProps) {
  return (
    <div
      className="flex min-h-screen items-center justify-center p-4 sm:p-6 relative overflow-hidden"
      style={{ background: 'var(--bg-base, #22252A)' }}
    >
      {/* Декоративное жидкое свечение заднего плана */}
      <div className="ambient-glow" aria-hidden>
        <div className="ambient-orb-1" />
        <div className="ambient-orb-2" />
      </div>

      <div className="z-10 w-full max-w-md glass-card rounded-3xl p-6 sm:p-8 space-y-6 text-center shadow-2xl border border-border/60">
        {/* Иконка блокировки обычного браузера */}
        <div className="relative mx-auto w-20 h-20 rounded-3xl bg-destructive-surface border border-destructive/30 flex items-center justify-center text-destructive shadow-xl shadow-destructive/15">
          <Smartphone className="w-10 h-10 stroke-[1.8]" />
          <div className="absolute -bottom-1 -right-1 w-7 h-7 rounded-full bg-base border-2 border-card flex items-center justify-center text-destructive shadow">
            <ShieldAlert className="w-4 h-4 stroke-[2.2]" />
          </div>
        </div>

        {/* Заголовок и пояснение */}
        <div className="space-y-2">
          <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-primary-surface border border-primary/25 text-primary text-xs font-heading font-bold uppercase tracking-wider mb-1">
            <Lock className="w-3.5 h-3.5" />
            <span>Только для мессенджеров</span>
          </div>
          <h1 className="font-heading font-extrabold text-2xl sm:text-[26px] text-foreground tracking-tight leading-snug">
            Вход через браузер запрещён
          </h1>
          <p className="text-sm sm:text-base text-neutral leading-relaxed max-w-sm mx-auto">
            {message ||
              'Расписание МИСИС работает исключительно как Mini App внутри Telegram и ВКонтакте. Прямой доступ через стандартный браузер ограничен в целях безопасности.'}
          </p>
        </div>

        {/* Кнопки перехода в официальные боты / приложения */}
        <div className="space-y-3 pt-2">
          <a
            href={telegramUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="w-full flex items-center justify-between py-3.5 px-5 rounded-2xl bg-primary hover:bg-primary-hover text-white font-heading font-bold text-base shadow-lg shadow-primary/30 transition-all duration-200 active:scale-[0.98] cursor-pointer group"
          >
            <div className="flex items-center gap-3">
              <TelegramIcon className="w-6 h-6 text-white flex-shrink-0" />
              <span>Открыть в Telegram</span>
            </div>
            <ArrowUpRight className="w-5 h-5 opacity-80 group-hover:translate-x-0.5 group-hover:-translate-y-0.5 transition-transform" />
          </a>

          <a
            href={vkUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="w-full flex items-center justify-between py-3.5 px-5 rounded-2xl bg-card hover:bg-neutral-surface border border-border text-foreground font-heading font-bold text-base transition-all duration-200 active:scale-[0.98] cursor-pointer group"
          >
            <div className="flex items-center gap-3">
              <div className="w-6 h-6 rounded-lg bg-primary-surface text-primary flex items-center justify-center flex-shrink-0">
                <VkIcon className="w-4 h-4" />
              </div>
              <span>Открыть ВКонтакте</span>
            </div>
            <ArrowUpRight className="w-5 h-5 text-neutral group-hover:translate-x-0.5 group-hover:-translate-y-0.5 transition-transform" />
          </a>
        </div>

        {/* Сноска о безопасности */}
        <div className="pt-2 border-t border-border/40 flex items-center justify-center gap-2 text-xs text-neutral/70 font-medium">
          <Lock className="w-3.5 h-3.5 flex-shrink-0" />
          <span>Доступ защищён сквозной HMAC-верификацией</span>
        </div>
      </div>
    </div>
  )
}
