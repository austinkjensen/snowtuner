/**
 * Layout route for /queries/*.
 *
 * In TanStack file-based routing, dotted filenames form parent/child pairs:
 *   queries.tsx          → layout (this file; renders <Outlet />)
 *   queries.index.tsx    → /queries        (the explorer)
 *
 * Keeping a layout from the start leaves room to add child routes later
 * (e.g. queries.$id, queries.families.$hash) without a refactor.
 */
import { createFileRoute, Outlet } from '@tanstack/react-router'

export const Route = createFileRoute('/queries')({
  component: QueriesLayout,
})

function QueriesLayout() {
  return <Outlet />
}
