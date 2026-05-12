import { createFileRoute, Link } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { AlertCircle, ArrowDown, ArrowUp, ArrowUpDown, Search } from 'lucide-react'
import { api, type Warehouse } from '@/lib/api'
import { formatNumber } from '@/lib/format'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

export const Route = createFileRoute('/warehouses/')({
  component: Warehouses,
})

type SortKey =
  | 'name'
  | 'size'
  | 'auto_suspend_seconds'
  | 'queries_in_window'
  | 'suspend_resume_events'
  | 'open_proposals'
type SortDir = 'asc' | 'desc'

function Warehouses() {
  const warehouses = useQuery({ queryKey: ['warehouses'], queryFn: api.warehouses })
  const proposed = useQuery({
    queryKey: ['recommendations', 'PROPOSED'],
    queryFn: () => api.listRecommendations({ status: 'PROPOSED', limit: 500 }),
  })
  const autoConfig = useQuery({
    queryKey: ['autonomous-config'],
    queryFn: api.listAutonomousConfig,
  })

  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('queries_in_window')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  const proposalsByWarehouse = useMemo(() => {
    const m = new Map<string, number>()
    for (const r of proposed.data ?? []) {
      const name = warehouseFromTarget(r.target_resource)
      if (!name) continue
      m.set(name, (m.get(name) ?? 0) + 1)
    }
    return m
  }, [proposed.data])

  const enabledByWarehouse = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const c of autoConfig.data ?? []) {
      if (!c.enabled || c.warehouse_name === '*') continue
      const list = m.get(c.warehouse_name) ?? []
      list.push(c.action_type)
      m.set(c.warehouse_name, list)
    }
    return m
  }, [autoConfig.data])

  const rows = useMemo(() => {
    const all = (warehouses.data ?? []).map((w) => ({
      ...w,
      open_proposals: proposalsByWarehouse.get(w.name) ?? 0,
      autonomous_action_types: enabledByWarehouse.get(w.name) ?? [],
    }))
    const filtered = search
      ? all.filter((w) => w.name.toLowerCase().includes(search.toLowerCase()))
      : all
    return [...filtered].sort((a, b) => compareRows(a, b, sortKey, sortDir))
  }, [warehouses.data, proposalsByWarehouse, enabledByWarehouse, search, sortKey, sortDir])

  if (warehouses.isError) return <ErrorState />

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir(defaultDirFor(key))
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Warehouses</h1>
        <p className="text-sm text-muted-foreground">
          Click any warehouse to open its command center: open proposals, autonomous-mode
          settings, application history, and activity.
        </p>
      </div>

      <Card>
        <CardHeader className="p-4">
          <div className="flex items-center gap-3">
            <Search className="h-4 w-4 text-muted-foreground" />
            <input
              type="text"
              placeholder="Search warehouses…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="flex-1 bg-transparent text-sm placeholder:text-muted-foreground focus:outline-none"
            />
            <span className="text-xs text-muted-foreground tabular-nums">
              {rows.length} {rows.length === 1 ? 'warehouse' : 'warehouses'}
            </span>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {warehouses.isLoading ? (
            <SkeletonTable />
          ) : rows.length === 0 ? (
            <p className="px-6 py-12 text-center text-sm text-muted-foreground">
              {warehouses.data?.length === 0
                ? 'No warehouses ingested. Run `snowtuner sync` to pull from your Snowflake account.'
                : 'No warehouses match the search.'}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <Th onClick={() => toggleSort('name')} active={sortKey === 'name'} dir={sortDir}>Name</Th>
                    <Th onClick={() => toggleSort('size')} active={sortKey === 'size'} dir={sortDir}>Size</Th>
                    <Th
                      onClick={() => toggleSort('auto_suspend_seconds')}
                      active={sortKey === 'auto_suspend_seconds'}
                      dir={sortDir}
                      align="right"
                    >
                      Auto-suspend
                    </Th>
                    <Th
                      onClick={() => toggleSort('queries_in_window')}
                      active={sortKey === 'queries_in_window'}
                      dir={sortDir}
                      align="right"
                    >
                      Queries (14d)
                    </Th>
                    <Th
                      onClick={() => toggleSort('suspend_resume_events')}
                      active={sortKey === 'suspend_resume_events'}
                      dir={sortDir}
                      align="right"
                    >
                      Cycles
                    </Th>
                    <th className="px-4 py-2.5 text-center font-medium">Auto</th>
                    <Th
                      onClick={() => toggleSort('open_proposals')}
                      active={sortKey === 'open_proposals'}
                      dir={sortDir}
                      align="right"
                    >
                      Open
                    </Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {rows.map((row) => (
                    <Row key={row.name} row={row} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function Row({
  row,
}: {
  row: Warehouse & { open_proposals: number; autonomous_action_types: string[] }
}) {
  return (
    <tr className="group transition-colors hover:bg-muted/40">
      <td className="px-4 py-2.5">
        <Link
          to="/warehouses/$name"
          params={{ name: row.name }}
          className="font-medium underline-offset-4 group-hover:underline"
        >
          {row.name}
        </Link>
      </td>
      <td className="px-4 py-2.5 text-muted-foreground">{row.size ?? '—'}</td>
      <td className="px-4 py-2.5 text-right tabular-nums text-muted-foreground">
        {row.auto_suspend_seconds == null ? '—' : `${row.auto_suspend_seconds}s`}
      </td>
      <td className="px-4 py-2.5 text-right tabular-nums">
        {formatNumber(row.queries_in_window)}
      </td>
      <td className="px-4 py-2.5 text-right tabular-nums text-muted-foreground">
        {formatNumber(row.suspend_resume_events)}
      </td>
      <td className="px-4 py-2.5 text-center">
        {row.autonomous_action_types.length > 0 ? (
          <span
            className="inline-block h-2 w-2 rounded-full bg-success"
            title={`Autonomous enabled: ${row.autonomous_action_types.join(', ')}`}
          />
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        )}
      </td>
      <td className="px-4 py-2.5 text-right">
        {row.open_proposals > 0 ? (
          <Badge variant="outline" className="tabular-nums">
            {row.open_proposals}
          </Badge>
        ) : (
          <span className="text-xs text-muted-foreground">0</span>
        )}
      </td>
    </tr>
  )
}

function Th({
  children,
  onClick,
  active,
  dir,
  align,
}: {
  children: React.ReactNode
  onClick: () => void
  active: boolean
  dir: SortDir
  align?: 'right'
}) {
  return (
    <th
      className={cn(
        'cursor-pointer select-none px-4 py-2.5 font-medium hover:text-foreground',
        align === 'right' && 'text-right',
      )}
      onClick={onClick}
    >
      <span className={cn('inline-flex items-center gap-1', align === 'right' && 'justify-end')}>
        {children}
        {active ? (
          dir === 'asc' ? <ArrowUp className="h-3 w-3" /> : <ArrowDown className="h-3 w-3" />
        ) : (
          <ArrowUpDown className="h-3 w-3 opacity-40" />
        )}
      </span>
    </th>
  )
}

function SkeletonTable() {
  return (
    <div className="divide-y divide-border">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="px-4 py-3">
          <div className="h-4 w-1/2 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  )
}

function ErrorState() {
  return (
    <Card className="border-destructive/40">
      <CardHeader className="flex-row items-start gap-3 p-6">
        <AlertCircle className="h-5 w-5 text-destructive" />
        <div>
          <CardTitle className="text-foreground">Couldn't load warehouses</CardTitle>
          <p className="mt-1 text-sm text-muted-foreground">
            Is <code className="font-mono">snowtuner api</code> running?
          </p>
        </div>
      </CardHeader>
    </Card>
  )
}

// ── helpers ────────────────────────────────────────────────────────────────

function warehouseFromTarget(target: string | null | undefined): string | null {
  if (!target) return null
  const m = target.match(/^warehouse:([^:]+)/)
  return m ? m[1] : null
}

function compareRows(
  a: Warehouse & { open_proposals: number },
  b: Warehouse & { open_proposals: number },
  key: SortKey,
  dir: SortDir,
): number {
  const av = (a as Record<string, unknown>)[key]
  const bv = (b as Record<string, unknown>)[key]
  let cmp: number
  if (av == null && bv == null) cmp = 0
  else if (av == null) cmp = 1
  else if (bv == null) cmp = -1
  else if (typeof av === 'number' && typeof bv === 'number') cmp = av - bv
  else cmp = String(av).localeCompare(String(bv))
  return dir === 'asc' ? cmp : -cmp
}

function defaultDirFor(key: SortKey): SortDir {
  // Strings → asc by default; numbers → desc (most-active first).
  if (key === 'name' || key === 'size') return 'asc'
  return 'desc'
}
