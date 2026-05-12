import { Outlet, createRootRoute } from '@tanstack/react-router'
import { TopNav } from '@/components/top-nav'

export const Route = createRootRoute({
  component: RootLayout,
  notFoundComponent: NotFound,
})

function RootLayout() {
  return (
    <div className="min-h-svh bg-background text-foreground">
      <TopNav />
      <main className="mx-auto max-w-7xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  )
}

function NotFound() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-2">
      <h1 className="text-2xl font-semibold">Not found</h1>
      <p className="text-sm text-muted-foreground">
        Nothing here. Check the URL or head back to the dashboard.
      </p>
    </div>
  )
}
