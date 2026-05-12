import { Link, useRouterState } from '@tanstack/react-router'
import { Moon, Settings, Snowflake, Sun } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useTheme } from '@/components/theme-provider'
import { cn } from '@/lib/utils'

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard' },
  { to: '/warehouses', label: 'Warehouses' },
  { to: '/recommendations', label: 'Recommendations' },
  { to: '/experiments', label: 'Experiments' },
] as const

export function TopNav() {
  const { resolved, setTheme } = useTheme()
  const { location } = useRouterState()
  const isActive = (to: string) =>
    to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)

  return (
    <header className="sticky top-0 z-40 border-b bg-background/95 backdrop-blur supports-[backdrop-filter]:bg-background/80">
      <div className="mx-auto flex h-14 max-w-7xl items-center px-6">
        <Link to="/" className="flex items-center gap-2 font-semibold">
          <Snowflake className="h-5 w-5 text-primary/80" aria-hidden />
          <span>snowtuner</span>
        </Link>

        <nav className="ml-8 flex items-center gap-1 text-sm">
          {NAV_ITEMS.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className={cn(
                'rounded-md px-3 py-1.5 transition-colors',
                isActive(item.to)
                  ? 'bg-secondary text-secondary-foreground'
                  : 'text-muted-foreground hover:bg-accent hover:text-accent-foreground',
              )}
            >
              {item.label}
            </Link>
          ))}
        </nav>

        <div className="ml-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            aria-label="Toggle theme"
            onClick={() => setTheme(resolved === 'dark' ? 'light' : 'dark')}
          >
            {resolved === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          </Button>
          <Button variant="ghost" size="icon" asChild aria-label="Settings">
            <Link to="/settings">
              <Settings className="h-4 w-4" />
            </Link>
          </Button>
        </div>
      </div>
    </header>
  )
}
