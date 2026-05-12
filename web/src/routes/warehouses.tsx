/**
 * Layout route for /warehouses/*.
 *
 * In TanStack file-based routing, dotted filenames form parent/child pairs:
 *   warehouses.tsx          → layout (this file; renders <Outlet />)
 *   warehouses.index.tsx    → /warehouses           (the list)
 *   warehouses.$name.tsx    → /warehouses/$name     (the detail)
 *
 * Without an Outlet here, child routes never render — what users would see is
 * the layout content for both the list and the detail URLs.  This file is
 * intentionally minimal: any chrome shared by the list and detail (e.g. a
 * "Warehouses" breadcrumb) would live here later.
 */
import { createFileRoute, Outlet } from '@tanstack/react-router'

export const Route = createFileRoute('/warehouses')({
  component: WarehousesLayout,
})

function WarehousesLayout() {
  return <Outlet />
}
