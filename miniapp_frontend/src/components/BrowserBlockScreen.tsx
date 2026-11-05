import { ArrowUpRight, Check, Lock, ShieldAlert, Smartphone } from 'lucide-react'
import { TelegramIcon, VkIcon } from './PlatformIcons'

interface BrowserBlockScreenProps {
  message?: string
  telegramUrl?: string
  vkUrl?: string
}

const DEFAULT_TG_URL =
  import.meta.env.VITE_TELEGRAM_BOT_URL || 'https://t.me/raspnekotybot'
const DEFAULT_VK_URL =
  import.meta.env.VITE_VK_BOT_URL || 'https://vk.ru/nekotyopk'

export function BrowserBlockScreen({
  message,
  telegramUrl = DEFAULT_TG_URL,
  vkUrl = DEFAULT_VK_URL,
}: BrowserBlockScreenProps) {
  return (
    <div
      className="flex min-h-screen w-full items-center justify-center p-4 sm:p-6 relative overflow-hidden select-none"
      style={{
        background:
          'radial-gradient(ellipse 85% 55% at 50% 0%, var(--brand-glow), transparent 75%), var(--bg-gradient, var(--bg-base, #22252A))',
      }}
    >
      {/* Декоративное жидкое свечение заднего плана */}
      <div className="ambient-glow" aria-hidden="true">
        <div className="ambient-orb-1 opacity-70" />
        <div className="ambient-orb-2 opacity-60" />
      </div>

      <div className="glass-card rounded-[2rem] p-6 sm:p-8 max-w-md w-full relative z-10 shadow-2xl border border-border/80 space-y-6 text-center backdrop-blur-2xl">
        {/* Иконка устройства / замка */}
        <div className="relative inline-flex items-center justify-center mx-auto">
          <div className="w-20 h-20 rounded-3xl bg-error/15 border border-error/30 flex items-center justify-center text-error shadow-lg shadow-error/20">
            <Smartphone className="w-10 h-10 stroke-[1.75]" />
          </div>
          <div className="absolute -bottom-1 -right-1 w-7 h-7 rounded-full bg-base border-2 border-border flex items-center justify-center text-error shadow-md">
            <Lock className="w-3.5 h-3.5" />
          </div>
        </div>

        {/* Заголовок и пояснение */}
        <div className="space-y-2">
          <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-primary/10 border border-primary/25 text-primary text-xs font-heading font-semibold uppercase tracking-wider">
            <Lock className="w-3 h-3" />
            <span>Только для мессенджеров</span>
          </div>

          <h1 className="font-heading font-extrabold text-2xl sm:text-3xl text-foreground tracking-tight leading-tight pt-1">
            Вход через браузер запрещён
          </h1>

          <p className="font-sans text-sm sm:text-base text-foreground/75 leading-relaxed max-w-sm mx-auto">
            {message || (
              <>
                Вход через обычный браузер запрещён. Откройте приложение через Telegram или ВКонтакте.
              </>
            )}
          </p>
        </div>

        {/* Информационные пункты */}
        <div className="text-left bg-surface/60 border border-border/60 rounded-2xl p-4 space-y-2.5 text-xs text-foreground/85 font-sans">
          <div className="flex items-center gap-2.5">
            <div className="w-5 h-5 rounded-full bg-success/15 text-success flex items-center justify-center flex-shrink-0">
              <Check className="w-3 h-3 stroke-[3]" />
            </div>
            <span>Расписание групп, преподавателей и аудиторий</span>
          </div>
          <div className="flex items-center gap-2.5">
            <div className="w-5 h-5 rounded-full bg-success/15 text-success flex items-center justify-center flex-shrink-0">
              <Check className="w-3 h-3 stroke-[3]" />
            </div>
            <span>Мгновенные push-уведомления об отменах и заменах</span>
          </div>
        </div>

        {/* Кнопки перехода в Telegram и ВКонтакте */}
        <div className="space-y-3 pt-1">
          <a
            href={telegramUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="w-full flex items-center justify-between py-3.5 px-5 rounded-2xl bg-gradient-to-r from-[#2AABEE] to-[#229ED9] hover:from-[#249bd9] hover:to-[#1e8bc0] text-white font-heading font-bold text-base shadow-lg shadow-[#2AABEE]/25 transition-all duration-200 active:scale-[0.98] cursor-pointer group"
          >
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-xl bg-white/20 flex items-center justify-center flex-shrink-0">
                <TelegramIcon className="w-5 h-5 text-white" />
              </div>
              <span>Открыть в Telegram</span>
            </div>
            <ArrowUpRight className="w-5 h-5 opacity-90 group-hover:translate-x-0.5 group-hover:-translate-y-0.5 transition-transform" />
          </a>

          <a
            href={vkUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="w-full flex items-center justify-between py-3.5 px-5 rounded-2xl bg-gradient-to-r from-[#0077FF] to-[#0066DD] hover:from-[#006bd6] hover:to-[#005bb8] text-white font-heading font-bold text-base shadow-lg shadow-[#0077FF]/25 transition-all duration-200 active:scale-[0.98] cursor-pointer group"
          >
            <div className="flex items-center gap-3">
              <div className="w-8 h-8 rounded-xl bg-white/20 flex items-center justify-center flex-shrink-0">
                <VkIcon className="w-5 h-5 text-white" />
              </div>
              <span>Открыть ВКонтакте</span>
            </div>
            <ArrowUpRight className="w-5 h-5 opacity-90 group-hover:translate-x-0.5 group-hover:-translate-y-0.5 transition-transform" />
          </a>
        </div>

        {/* Сноска о безопасности */}
        <div className="pt-2 border-t border-border/50 flex items-center justify-center gap-2 text-xs text-foreground/60 font-sans font-medium">
          <ShieldAlert className="w-3.5 h-3.5 text-primary flex-shrink-0" />
          <span>Доступ защищён сквозной HMAC-верификацией</span>
        </div>
      </div>
    </div>
  )
}
