import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { AlertCircle, ArrowDown, ArrowUp, ArrowUpDown, Search } from 'lucide-react'
import { api, type Recommendation, type RecommendationStatus } from '@/lib/api'
import { creditsDelta } from '@/lib/format'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { RecommendationDrawer } from '@/components/recommendation-drawer'

const ALL_STATUSES: RecommendationStatus[] = [
  'PROPOSED',
  'ACCEPTED',
  'REJECTED',
  'APPLIED',
  'ROLLED_BACK',
  'SUPERSEDED',
]

type RecommendationsSearch = {
  status?: RecommendationStatus
  id?: number
}

export const Route = createFileRoute('/recommendations')({
  validateSearch: (search: Record<string, unknown>): RecommendationsSearch => {
    const s = String(search.status ?? '').toUpperCase()
    const status = (ALL_STATUSES as string[]).includes(s)
      ? (s as RecommendationStatus)
      : undefined
    const idRaw = search.id
    const id =
      typeof idRaw === 'number'
        ? idRaw
        : typeof idRaw === 'string' && /^\d+$/.test(idRaw)
        ? Number(idRaw)
        : undefined
    return { status, id }
  },
  component: Recommendations,
})

type SortKey = 'id' | 'warehouse' | 'action_type' | 'confidence' | 'credits'
type SortDir = 'asc' | 'desc'

function Recommendations() {
  const search = Route.useSearch()
  const navigate = useNavigate({ from: '/recommendations' })
  const status: RecommendationStatus = search.status ?? 'PROPOSED'

  const list = useQuery({
    queryKey: ['recommendations', status],
    queryFn: () => api.listRecommendations({ status, limit: 500 }),
  })

  const [searchText, setSearchText] = useState('')
  const [sortKey, setSortKey] = useState<SortKey>('id')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  const rows = useMemo(() => {
    const all = (list.data ?? []).map((r) => ({
      ...r,
      warehouse: warehouseFromTarget(r.target_resource) ?? '',
      confidence: r.expected_impact?.confidence ?? 0,
      credits: r.expected_impact?.credits_delta_daily ?? null,
    }))
    const filtered = searchText
      ? all.filter((r) =>
          r.warehouse.toLowerCase().includes(searchText.toLowerCase()) ||
          r.action_type.toLowerCase().includes(searchText.toLowerCase()),
        )
      : all
    return [...filtered].sort((a, b) => compareRows(a, b, sortKey, sortDir))
  }, [list.data, searchText, sortKey, sortDir])

  function setStatus(s: RecommendationStatus) {
    navigate({ search: { status: s, id: search.id } })
  }
  function openRec(id: number) {
    navigate({ search: { status, id } })
  }
  function closeRec() {
    navigate({ search: { status, id: undefined } })
  }
  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(key)
      setSortDir(defaultDirFor(key))
    }
  }

  if (list.isError) return <ErrorState />

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Recommendations</h1>
        <p className="text-sm text-muted-foreground">
          Cross-warehouse triage. Click any row for full detail, evidence, and the SQL to run.
        </p>
      </div>

      <Card>
        <CardHeader className="p-4">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-2">
              <span className="text-xs uppercase tracking-wide text-muted-foreground">Status</span>
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value as RecommendationStatus)}
                className="rounded-md border bg-background px-2 py-1 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
              >
                {ALL_STATUSES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <div className="flex flex-1 items-center gap-2">
              <Search className="h-4 w-4 text-muted-foreground" />
              <input
                type="text"
                placeholder="Search by warehouse or type…"
                value={searchText}
                onChange={(e) => setSearchText(e.target.value)}
                className="flex-1 bg-transparent text-sm placeholder:text-muted-foreground focus:outline-none"
              />
            </div>
            <span className="text-xs text-muted-foreground tabular-nums">
              {rows.length} {rows.length === 1 ? 'rec' : 'recs'}
            </span>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {list.isLoading ? (
            <SkeletonTable />
          ) : rows.length === 0 ? (
            <p className="px-6 py-12 text-center text-sm text-muted-foreground">
              {(list.data ?? []).length === 0
                ? `No recommendations with status ${status}.`
                : 'No recommendations match the search.'}
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b bg-muted/30 text-xs uppercase tracking-wide text-muted-foreground">
                  <tr>
                    <Th onClick={() => toggleSort('id')} active={sortKey === 'id'} dir={sortDir}>
                      ID
                    </Th>
                    <Th
                      onClick={() => toggleSort('warehouse')}
                      active={sortKey === 'warehouse'}
                      dir={sortDir}
                    >
                      Warehouse
                    </Th>
                    <Th
                      onClick={() => toggleSort('action_type')}
                      active={sortKey === 'action_type'}
                      dir={sortDir}
                    >
                      Type
                    </Th>
                    <th className="px-4 py-2.5 font-medium">Proposal</th>
                    <Th
                      onClick={() => toggleSort('confidence')}
                      active={sortKey === 'confidence'}
                      dir={sortDir}
                      align="right"
                    >
                      Conf
                    </Th>
                    <Th
                      onClick={() => toggleSort('credits')}
                      active={sortKey === 'credits'}
                      dir={sortDir}
                      align="right"
                    >
                      Cred/day
                    </Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {rows.map((row) => (
                    <Row
                      key={row.id ?? 0}
                      row={row}
                      onOpen={() => row.id != null && openRec(row.id)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <RecommendationDrawer
        recommendationId={search.id ?? null}
        onOpenChange={(open) => {
          if (!open) closeRec()
        }}
      />
    </div>
  )
}

function Row({
  row,
  onOpen,
}: {
  row: Recommendation & { warehouse: string; confidence: number; credits: number | null }
  onOpen: () => void
}) {
  return (
    <tr className="cursor-pointer transition-colors hover:bg-muted/40" onClick={onOpen}>
      <td className="px-4 py-2.5 font-mono text-xs text-muted-foreground">#{row.id}</td>
      <td className="px-4 py-2.5 font-medium">{row.warehouse || '—'}</td>
      <td className="px-4 py-2.5 text-muted-foreground">{row.action_type}</td>
      <td className="px-4 py-2.5">
        <span className="line-clamp-1 text-foreground/90">
          {firstLine(row.preview)}
        </span>
      </td>
      <td className="px-4 py-2.5 text-right tabular-nums">
        <ConfidenceBadge value={row.confidence} />
      </td>
      <td className="px-4 py-2.5 text-right tabular-nums text-muted-foreground">
        {creditsDelta(row.credits)}
      </td>
    </tr>
  )
}

function ConfidenceBadge({ value }: { value: number }) {
  const variant: 'success' | 'warning' | 'outline' =
    value >= 0.85 ? 'success' : value >= 0.6 ? 'warning' : 'outline'
  return (
    <Badge variant={variant} className="tabular-nums">
      {value.toFixed(2)}
    </Badge>
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
          <CardTitle className="text-foreground">Couldn't load recommendations</CardTitle>
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

function firstLine(text: string | null | undefined): string {
  if (!text) return ''
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed && !trimmed.startsWith('--')) return trimmed
  }
  return text
}

function compareRows(
  a: Recommendation & { warehouse: string; confidence: number; credits: number | null },
  b: Recommendation & { warehouse: string; confidence: number; credits: number | null },
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
  if (key === 'warehouse' || key === 'action_type') return 'asc'
  return 'desc'
}
