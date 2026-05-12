import * as React from 'react'

type Theme = 'dark' | 'light' | 'system'

type ThemeProviderState = {
  theme: Theme
  setTheme: (theme: Theme) => void
  resolved: 'dark' | 'light'
}

const ThemeProviderContext = React.createContext<ThemeProviderState | undefined>(undefined)

const STORAGE_KEY = 'snowtuner-theme'

function applyTheme(theme: Theme): 'dark' | 'light' {
  const root = document.documentElement
  root.classList.remove('light')
  let resolved: 'dark' | 'light' = 'dark'
  if (theme === 'light') {
    root.classList.add('light')
    resolved = 'light'
  } else if (theme === 'system') {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
    if (!prefersDark) {
      root.classList.add('light')
      resolved = 'light'
    }
  }
  return resolved
}

export function ThemeProvider({
  children,
  defaultTheme = 'dark',
}: {
  children: React.ReactNode
  defaultTheme?: Theme
}) {
  const [theme, setThemeState] = React.useState<Theme>(() => {
    if (typeof window === 'undefined') return defaultTheme
    return (localStorage.getItem(STORAGE_KEY) as Theme | null) ?? defaultTheme
  })
  const [resolved, setResolved] = React.useState<'dark' | 'light'>(() => applyTheme(theme))

  React.useEffect(() => {
    setResolved(applyTheme(theme))
    if (theme !== 'system') return
    // Re-apply when the OS theme changes
    const mql = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => setResolved(applyTheme(theme))
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [theme])

  const setTheme = React.useCallback((t: Theme) => {
    localStorage.setItem(STORAGE_KEY, t)
    setThemeState(t)
  }, [])

  return (
    <ThemeProviderContext.Provider value={{ theme, setTheme, resolved }}>
      {children}
    </ThemeProviderContext.Provider>
  )
}

export function useTheme(): ThemeProviderState {
  const ctx = React.useContext(ThemeProviderContext)
  if (!ctx) throw new Error('useTheme must be used within a ThemeProvider')
  return ctx
}
