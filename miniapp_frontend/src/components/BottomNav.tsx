import { CalendarDays, History, User } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { usePlatform } from '../hooks/usePlatform'
import { cn } from '../lib/utils'

interface NavItem {
  to: string
  label: string
  icon: typeof CalendarDays
}

const navItems: NavItem[] = [
  { to: '/schedule', label: 'Расписание', icon: CalendarDays },
  { to: '/history', label: 'История', icon: History },
  { to: '/profile', label: 'Профиль', icon: User },
]

interface BottomNavProps {
  safeAreaBottom?: number
}

export function BottomNav({ safeAreaBottom = 0 }: BottomNavProps) {
  const platform = usePlatform()

  return (
    <nav
      className="fixed bottom-0 left-0 right-0 z-50 pointer-events-none px-4 pb-[env(safe-area-inset-bottom,8px)]"
      style={{ paddingBottom: safeAreaBottom ? `${safeAreaBottom}px` : undefined }}
    >
      <div className="pointer-events-auto max-w-md mx-auto glass-dock rounded-2xl p-1.5 shadow-2xl">
        <div className="flex items-center justify-around gap-1">
          {navItems.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                onClick={() => platform.hapticFeedback('light')}
                className={({ isActive }) =>
                  cn(
                    'flex-1 flex flex-col items-center justify-center gap-1 py-2 px-3 rounded-xl transition-all duration-200',
                    isActive
                      ? 'bg-primary text-white font-semibold shadow-lg shadow-primary/30'
                      : 'text-muted-foreground hover:text-foreground hover:bg-black/5 dark:hover:bg-white/5',
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <Icon
                      className={cn(
                        'w-5 h-5 transition-transform duration-200',
                        isActive ? 'scale-110 stroke-[2.2]' : 'stroke-[1.8]',
                      )}
                    />
                    <span className="text-[11px] font-medium leading-none tracking-tight">
                      {item.label}
                    </span>
                  </>
                )}
              </NavLink>
            )
          })}
        </div>
      </div>
    </nav>
  )
}
