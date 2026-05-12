/**
 * Layout route for /experiments/*.
 *
 * In TanStack file-based routing, dotted filenames form parent/child pairs:
 *   experiments.tsx          → layout (this file; renders <Outlet />)
 *   experiments.index.tsx    → /experiments        (the list)
 *   experiments.$id.tsx      → /experiments/$id    (the detail)
 *
 * Without an Outlet here, child routes never render — what users would see is
 * the layout content for both the list and the detail URLs.  This file is
 * intentionally minimal: any chrome shared by the list and detail would
 * live here later.
 */
import { createFileRoute, Outlet } from '@tanstack/react-router'

export const Route = createFileRoute('/experiments')({
  component: ExperimentsLayout,
})

function ExperimentsLayout() {
  return <Outlet />
}
