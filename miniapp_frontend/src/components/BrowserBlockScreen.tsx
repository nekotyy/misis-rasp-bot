import { ArrowUpRight, Check, Lock, ShieldAlert, Smartphone } from 'lucide-react'
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
      className="flex min-h-screen w-full items-center justify-center p-4 sm:p-6 relative overflow-hidden select-none"
      style={{
        background:
          'radial-gradient(ellipse 85% 55% at 50% 0%, var(--brand-glow), transparent 75%), var(--bg-gradient, var(--bg-base, #22252A))',
      }}
    >
      {/* Декоративное жидкое свечение заднего плана */}
      <div className="ambient-glow" aria-hidden="true">
        <div
          className="ambient-orb-1"
          style={{ opacity: 0.25, width: '480px', height: '480px' }}
        />
        <div
          className="ambient-orb-2"
          style={{ opacity: 0.2, width: '520px', height: '520px' }}
        />
      </div>

      <div className="z-10 w-full max-w-[440px] glass-card rounded-[28px] sm:rounded-[32px] p-6 sm:p-8 space-y-6 text-center shadow-2xl border border-border">
        {/* Иконка-бейдж доступа через мессенджеры */}
        <div className="relative mx-auto w-20 h-20 sm:w-22 sm:h-22 rounded-3xl bg-gradient-to-b from-primary/20 via-primary-surface to-primary/10 border border-primary/35 flex items-center justify-center text-primary shadow-xl shadow-primary/20">
          <Smartphone className="w-10 h-10 stroke-[1.8]" />
          <div className="absolute -bottom-1 -right-1 w-7 h-7 rounded-full bg-card border-2 border-primary/40 flex items-center justify-center text-primary shadow">
            <Lock className="w-3.5 h-3.5 stroke-[2.2]" />
          </div>
        </div>

        {/* Заголовок и пояснительный текст (Nekst + Raleway 16/24) */}
        <div className="space-y-3">
          <div className="inline-flex items-center gap-1.5 px-3.5 py-1.5 rounded-full bg-primary-surface/80 border border-primary/30 text-primary text-xs font-heading font-bold uppercase tracking-wider shadow-sm">
            <Lock className="w-3.5 h-3.5" />
            <span>Только для мессенджеров</span>
          </div>
          <h1 className="font-heading font-extrabold text-2xl sm:text-[28px] text-foreground tracking-tight leading-snug">
            Вход через браузер запрещён
          </h1>
          <p className="font-sans font-normal text-base leading-6 text-foreground/85 max-w-sm mx-auto">
            {message ||
              'Расписание МИСИС работает исключительно как Mini App внутри Telegram и ВКонтакте. Прямой доступ через стандартный браузер ограничен в целях безопасности.'}
          </p>
        </div>

        {/* Информационные плашки возможностей */}
        <div className="p-3.5 sm:p-4 rounded-2xl bg-base/50 border border-border/60 text-left space-y-2.5">
          <div className="flex items-center gap-2.5 text-xs sm:text-sm font-sans text-foreground/80">
            <div className="w-5 h-5 rounded-md bg-primary-surface text-primary flex items-center justify-center flex-shrink-0">
              <Check className="w-3.5 h-3.5 stroke-[2.5]" />
            </div>
            <span>Расписание групп, преподавателей и аудиторий</span>
          </div>
          <div className="flex items-center gap-2.5 text-xs sm:text-sm font-sans text-foreground/80">
            <div className="w-5 h-5 rounded-md bg-primary-surface text-primary flex items-center justify-center flex-shrink-0">
              <Check className="w-3.5 h-3.5 stroke-[2.5]" />
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
