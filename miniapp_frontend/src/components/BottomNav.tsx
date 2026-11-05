import { NavLink } from 'react-router-dom'
import { cn } from '../lib/utils'

interface NavItem {
  to: string
  label: string
  icon: string
}

const navItems: NavItem[] = [
  { to: '/schedule', label: 'Расписание', icon: '📅' },
  { to: '/history', label: 'История', icon: '📚' },
  { to: '/profile', label: 'Профиль', icon: '👤' },
]

interface BottomNavProps {
  safeAreaBottom?: number
}

export function BottomNav({ safeAreaBottom = 0 }: BottomNavProps) {
  return (
    <nav
      className="fixed bottom-0 left-0 right-0 z-50 bg-background/95 backdrop-blur-md border-t border-border"
      style={{ paddingBottom: `max(${safeAreaBottom}px, env(safe-area-inset-bottom, 0px))` }}
    >
      <div className="flex items-center justify-around">
        {navItems.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) =>
              cn(
                'flex flex-col items-center justify-center gap-0.5 py-2 px-4 min-w-[60px] transition-colors',
                isActive
                  ? 'text-primary'
                  : 'text-muted-foreground hover:text-foreground',
              )
            }
          >
            {({ isActive }) => (
              <>
                <span className="text-xl leading-none" role="img" aria-hidden>
                  {item.icon}
                </span>
                <span
                  className={cn(
                    'text-[10px] font-medium leading-none',
                    isActive ? 'text-primary' : 'text-muted-foreground',
                  )}
                >
                  {item.label}
                </span>
              </>
            )}
          </NavLink>
        ))}
      </div>
    </nav>
  )
}
