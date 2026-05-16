import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Database, Search, AlertCircle } from 'lucide-react'
import { api, type QueryListFilters, type QueryRow, type QueryFamily } from '@/lib/api'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'

type View = 'queries' | 'families'

type QueriesSearch = {
  view?: View
  warehouse?: string
  user?: string
  status?: string
  type?: string
  has_remote_spill?: boolean
  has_queueing?: boolean
  search?: string
  detail?: string                // open detail side-sheet for this query_id
  family?: string                // open queries filtered to this hash
}

export const Route = createFileRoute('/queries/')({
  validateSearch: (s: Record<string, unknown>): QueriesSearch => {
    const view = s.view === 'families' ? 'families' : 'queries'
    return {
      view,
      warehouse: typeof s.warehouse === 'string' ? s.warehouse : undefined,
      user: typeof s.user === 'string' ? s.user : undefined,
      status: typeof s.status === 'string' ? s.status : undefined,
      type: typeof s.type === 'string' ? s.type : undefined,
      has_remote_spill: s.has_remote_spill === 'true' ? true : undefined,
      has_queueing: s.has_queueing === 'true' ? true : undefined,
      search: typeof s.search === 'string' ? s.search : undefined,
      detail: typeof s.detail === 'string' ? s.detail : undefined,
      family: typeof s.family === 'string' ? s.family : undefined,
    }
  },
  component: QueriesExplorer,
})

const PAGE_SIZE = 50

function QueriesExplorer() {
  const search = Route.useSearch()
  const navigate = useNavigate({ from: '/queries/' })
  const [offset, setOffset] = useState(0)

  const facets = useQuery({
    queryKey: ['query-facets'],
    queryFn: () => api.queryFacets(30),
  })

  const filters: QueryListFilters = useMemo(
    () => ({
      warehouse: search.warehouse,
      user: search.user,
      status: search.status,
      query_type: search.type,
      has_remote_spill: search.has_remote_spill,
      has_queueing: search.has_queueing,
      search: search.search,
      parameterized_hash: search.family,
      limit: PAGE_SIZE,
      offset,
    }),
    [search, offset],
  )

  const queries = useQuery({
    queryKey: ['queries', filters],
    queryFn: () => api.listQueries(filters),
    enabled: search.view !== 'families',
  })

  const families = useQuery({
    queryKey: ['query-families', filters],
    queryFn: () =>
      api.listQueryFamilies({
        warehouse: filters.warehouse,
        user: filters.user,
        status: filters.status,
        query_type: filters.query_type,
        has_remote_spill: filters.has_remote_spill,
        search: filters.search,
        limit: 100,
      }),
    enabled: search.view === 'families',
  })

  function setSearchParam<K extends keyof QueriesSearch>(key: K, value: QueriesSearch[K]) {
    setOffset(0)
    navigate({ search: { ...search, [key]: value } })
  }

  function clearFilters() {
    setOffset(0)
    navigate({ search: { view: search.view } })
  }

  function openDetail(queryId: string) {
    navigate({ search: { ...search, detail: queryId } })
  }

  function closeDetail() {
    const { detail, ...rest } = search
    void detail
    navigate({ search: rest })
  }

  function drillIntoFamily(hash: string) {
    navigate({ search: { ...search, view: 'queries', family: hash } })
  }

  const hasActiveFilters =
    !!search.warehouse || !!search.user || !!search.status || !!search.type ||
    !!search.has_remote_spill || !!search.has_queueing || !!search.search ||
    !!search.family

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      {/* Header */}
      <div className="mb-6 flex items-center gap-3">
        <Database className="h-6 w-6 text-primary/80" />
        <h1 className="text-2xl font-semibold">Queries</h1>
        <span className="text-sm text-muted-foreground">
          Explore ingested query history; group by family
        </span>
      </div>

      {/* View toggle */}
      <div className="mb-4 inline-flex rounded-md border bg-muted/30 p-0.5">
        <button
          onClick={() => navigate({ search: { ...search, view: 'queries' } })}
          className={`rounded px-3 py-1.5 text-sm ${search.view !== 'families' ? 'bg-background shadow-sm' : 'text-muted-foreground'}`}
        >
          Queries
        </button>
        <button
          onClick={() => navigate({ search: { ...search, view: 'families' } })}
          className={`rounded px-3 py-1.5 text-sm ${search.view === 'families' ? 'bg-background shadow-sm' : 'text-muted-foreground'}`}
        >
          Families
        </button>
      </div>

      {/* Filter chips */}
      <Card className="mb-4">
        <CardContent className="space-y-3 py-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <div className="relative">
              <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                type="text"
                placeholder="search query text…"
                className="w-64 rounded-md border bg-background py-1.5 pl-7 pr-2 text-sm"
                value={search.search ?? ''}
                onChange={(e) => setSearchParam('search', e.target.value || undefined)}
              />
            </div>

            <FilterDropdown
              label="warehouse"
              value={search.warehouse}
              options={facets.data?.warehouses ?? []}
              onChange={(v) => setSearchParam('warehouse', v)}
            />
            <FilterDropdown
              label="user"
              value={search.user}
              options={facets.data?.users ?? []}
              onChange={(v) => setSearchParam('user', v)}
            />
            <FilterDropdown
              label="type"
              value={search.type}
              options={facets.data?.query_types ?? []}
              onChange={(v) => setSearchParam('type', v)}
            />
            <FilterDropdown
              label="status"
              value={search.status}
              options={facets.data?.execution_statuses ?? []}
              onChange={(v) => setSearchParam('status', v)}
            />

            <FilterToggle
              label="remote spill"
              active={search.has_remote_spill === true}
              onClick={() =>
                setSearchParam('has_remote_spill', search.has_remote_spill ? undefined : true)
              }
            />
            <FilterToggle
              label="queueing"
              active={search.has_queueing === true}
              onClick={() =>
                setSearchParam('has_queueing', search.has_queueing ? undefined : true)
              }
            />

            {search.family && (
              <Badge variant="secondary" className="gap-1">
                family={search.family.slice(0, 12)}…
                <button
                  className="ml-1 text-muted-foreground hover:text-foreground"
                  onClick={() => setSearchParam('family', undefined)}
                >
                  ×
                </button>
              </Badge>
            )}

            {hasActiveFilters && (
              <Button variant="ghost" size="sm" onClick={clearFilters}>
                Clear all
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Body */}
      {search.view === 'families' ? (
        <FamiliesTable
          rows={families.data ?? []}
          loading={families.isLoading}
          onDrill={drillIntoFamily}
        />
      ) : (
        <QueriesTable
          rows={queries.data?.rows ?? []}
          total={queries.data?.total ?? 0}
          loading={queries.isLoading}
          offset={offset}
          pageSize={PAGE_SIZE}
          onOffsetChange={setOffset}
          onOpenDetail={openDetail}
          onDrillIntoFamily={drillIntoFamily}
        />
      )}

      {/* Detail sheet */}
      <Sheet open={!!search.detail} onOpenChange={(open) => !open && closeDetail()}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
          {search.detail && <QueryDetailPanel queryId={search.detail} />}
        </SheetContent>
      </Sheet>
    </div>
  )
}

// ── Filter chip components ──────────────────────────────────────────

function FilterDropdown({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string | undefined
  options: string[]
  onChange: (v: string | undefined) => void
}) {
  return (
    <select
      className="rounded-md border bg-background px-2 py-1.5 text-sm"
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value || undefined)}
    >
      <option value="">{label}: all</option>
      {options.map((o) => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  )
}

function FilterToggle({
  label,
  active,
  onClick,
}: {
  label: string
  active: boolean
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className={`rounded-md border px-2 py-1.5 text-xs ${
        active ? 'border-primary bg-primary/10 text-primary' : 'border-input bg-background text-muted-foreground'
      }`}
    >
      {label}
    </button>
  )
}

// ── Queries table ──────────────────────────────────────────────────

function QueriesTable({
  rows,
  total,
  loading,
  offset,
  pageSize,
  onOffsetChange,
  onOpenDetail,
  onDrillIntoFamily,
}: {
  rows: QueryRow[]
  total: number
  loading: boolean
  offset: number
  pageSize: number
  onOffsetChange: (n: number) => void
  onOpenDetail: (id: string) => void
  onDrillIntoFamily: (hash: string) => void
}) {
  return (
    <Card>
      <CardContent className="p-0">
        {loading && <div className="p-4 text-sm text-muted-foreground">Loading…</div>}
        {!loading && rows.length === 0 && (
          <div className="p-8 text-center text-sm text-muted-foreground">
            No queries match these filters. Try clearing some.
          </div>
        )}
        {rows.length > 0 && (
          <>
            <table className="w-full text-sm">
              <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2">Query</th>
                  <th className="px-4 py-2">Warehouse</th>
                  <th className="px-4 py-2">User</th>
                  <th className="px-4 py-2">Status</th>
                  <th className="px-4 py-2 text-right">Elapsed</th>
                  <th className="px-4 py-2 text-right">Scanned</th>
                  <th className="px-4 py-2 text-right">Spill</th>
                  <th className="px-4 py-2">Started</th>
                  <th className="px-4 py-2">Family</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr
                    key={r.query_id}
                    className="cursor-pointer border-b hover:bg-muted/30"
                    onClick={() => onOpenDetail(r.query_id)}
                  >
                    <td className="px-4 py-2 max-w-md truncate font-mono text-xs">
                      {r.query_text_preview}
                    </td>
                    <td className="px-4 py-2">{r.warehouse_name ?? '—'}</td>
                    <td className="px-4 py-2">{r.user_name ?? '—'}</td>
                    <td className="px-4 py-2">
                      {r.execution_status === 'SUCCESS' ? (
                        <Badge variant="secondary">{r.execution_status}</Badge>
                      ) : (
                        <Badge variant="destructive">{r.execution_status ?? '—'}</Badge>
                      )}
                    </td>
                    <td className="px-4 py-2 text-right font-mono">
                      {fmtMs(r.total_elapsed_ms)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-xs">
                      {fmtBytes(r.bytes_scanned)}
                    </td>
                    <td className="px-4 py-2 text-right">
                      {r.bytes_spilled_to_remote && r.bytes_spilled_to_remote > 0 ? (
                        <span className="inline-flex items-center gap-1 text-destructive" title="Remote spill">
                          <AlertCircle className="h-3 w-3" />
                          remote
                        </span>
                      ) : r.bytes_spilled_to_local && r.bytes_spilled_to_local > 0 ? (
                        <span className="text-yellow-600" title="Local spill">local</span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground">
                      {fmtDt(r.start_time)}
                    </td>
                    <td className="px-4 py-2">
                      {r.query_parameterized_hash ? (
                        <button
                          className="font-mono text-xs text-primary hover:underline"
                          onClick={(e) => {
                            e.stopPropagation()
                            onDrillIntoFamily(r.query_parameterized_hash!)
                          }}
                        >
                          {r.query_parameterized_hash.slice(0, 8)}
                        </button>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {/* Pagination */}
            <div className="flex items-center justify-between border-t px-4 py-2 text-sm">
              <span className="text-muted-foreground">
                Showing {offset + 1}–{Math.min(offset + rows.length, total)} of {total}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => onOffsetChange(Math.max(0, offset - pageSize))}
                >
                  Prev
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset + pageSize >= total}
                  onClick={() => onOffsetChange(offset + pageSize)}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

// ── Families table ──────────────────────────────────────────────────

function FamiliesTable({
  rows,
  loading,
  onDrill,
}: {
  rows: QueryFamily[]
  loading: boolean
  onDrill: (hash: string) => void
}) {
  return (
    <Card>
      <CardContent className="p-0">
        {loading && <div className="p-4 text-sm text-muted-foreground">Loading…</div>}
        {!loading && rows.length === 0 && (
          <div className="p-8 text-center text-sm text-muted-foreground">
            No families match these filters.
          </div>
        )}
        {rows.length > 0 && (
          <table className="w-full text-sm">
            <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-4 py-2">Family / sample SQL</th>
                <th className="px-4 py-2 text-right">Count</th>
                <th className="px-4 py-2 text-right">Mean elapsed</th>
                <th className="px-4 py-2 text-right">p95 elapsed</th>
                <th className="px-4 py-2 text-right">Total elapsed</th>
                <th className="px-4 py-2 text-right">Spilled</th>
                <th className="px-4 py-2 text-right">Failed</th>
                <th className="px-4 py-2">Last seen</th>
                <th className="px-4 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((f) => (
                <tr key={f.query_parameterized_hash} className="border-b hover:bg-muted/30">
                  <td className="px-4 py-2 max-w-md">
                    <div className="font-mono text-xs text-muted-foreground">
                      {f.query_parameterized_hash.slice(0, 12)}…
                    </div>
                    <div className="truncate font-mono text-xs">{f.representative_sql}</div>
                  </td>
                  <td className="px-4 py-2 text-right">{f.occurrence_count}</td>
                  <td className="px-4 py-2 text-right font-mono">
                    {fmtMs(f.mean_elapsed_ms)}
                  </td>
                  <td className="px-4 py-2 text-right font-mono">
                    {fmtMs(f.p95_elapsed_ms)}
                  </td>
                  <td className="px-4 py-2 text-right font-mono">
                    {fmtMs(f.total_elapsed_ms)}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {f.n_spill_remote > 0 ? (
                      <span className="text-destructive">{f.n_spill_remote}</span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-right">
                    {f.n_failed > 0 ? (
                      <span className="text-destructive">{f.n_failed}</span>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-xs text-muted-foreground">
                    {fmtDt(f.last_seen)}
                  </td>
                  <td className="px-4 py-2">
                    <button
                      onClick={() => onDrill(f.query_parameterized_hash)}
                      className="text-xs text-primary hover:underline"
                    >
                      Drill in →
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </CardContent>
    </Card>
  )
}

// ── Detail side-sheet ───────────────────────────────────────────────

function QueryDetailPanel({ queryId }: { queryId: string }) {
  const detail = useQuery({
    queryKey: ['query', queryId],
    queryFn: () => api.getQuery(queryId),
  })

  if (detail.isLoading) return <div className="p-4 text-sm">Loading…</div>
  if (detail.error)
    return <div className="p-4 text-sm text-destructive">{String(detail.error)}</div>
  if (!detail.data) return null
  const d = detail.data

  return (
    <>
      <SheetHeader>
        <SheetTitle className="break-all font-mono text-base">{d.query_id}</SheetTitle>
      </SheetHeader>
      <div className="mt-4 space-y-4">
        <Card>
          <CardContent className="p-4">
            <h3 className="mb-2 text-xs uppercase text-muted-foreground">SQL</h3>
            <pre className="max-h-96 overflow-auto rounded bg-muted/40 p-3 font-mono text-xs">
              {d.query_text}
            </pre>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="grid grid-cols-2 gap-3 p-4 text-sm">
            <Field label="Type" value={d.query_type ?? '—'} />
            <Field label="Status" value={d.execution_status ?? '—'} />
            <Field label="User" value={d.user_name ?? '—'} />
            <Field label="Role" value={d.role_name ?? '—'} />
            <Field label="Warehouse" value={d.warehouse_name ?? '—'} />
            <Field label="Size" value={d.warehouse_size ?? '—'} />
            <Field label="Database" value={d.database_name ?? '—'} />
            <Field label="Schema" value={d.schema_name ?? '—'} />
            <Field label="Started" value={fmtDt(d.start_time)} />
            <Field label="Ended" value={fmtDt(d.end_time)} />
          </CardContent>
        </Card>

        <Card>
          <CardContent className="grid grid-cols-2 gap-3 p-4 text-sm">
            <Field label="Total elapsed" value={fmtMs(d.total_elapsed_ms)} />
            <Field label="Compilation" value={fmtMs(d.compilation_ms)} />
            <Field label="Execution" value={fmtMs(d.execution_ms)} />
            <Field label="Queued (overload)" value={fmtMs(d.queued_overload_ms)} />
            <Field label="Queued (provisioning)" value={fmtMs(d.queued_provisioning_ms)} />
            <Field label="Bytes scanned" value={fmtBytes(d.bytes_scanned)} />
            <Field label="Spilled (local)" value={fmtBytes(d.bytes_spilled_to_local)} />
            <Field label="Spilled (remote)" value={fmtBytes(d.bytes_spilled_to_remote)} />
          </CardContent>
        </Card>

        {d.query_parameterized_hash && (
          <Card>
            <CardContent className="p-4 text-sm">
              <h3 className="mb-2 text-xs uppercase text-muted-foreground">Family</h3>
              <code className="block break-all font-mono text-xs">
                {d.query_parameterized_hash}
              </code>
            </CardContent>
          </Card>
        )}
      </div>
    </>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase text-muted-foreground">{label}</div>
      <div className="font-mono">{value}</div>
    </div>
  )
}

// ── Formatting helpers ──────────────────────────────────────────────

function fmtMs(ms: number | null | undefined): string {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms}ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`
}

function fmtBytes(b: number | null | undefined): string {
  if (b == null) return '—'
  if (b === 0) return '0'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let v = b
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v < 10 ? 2 : v < 100 ? 1 : 0)} ${units[i]}`
}

function fmtDt(s: string | null | undefined): string {
  if (!s) return '—'
  const d = new Date(s)
  return d.toLocaleString(undefined, {
    year: '2-digit',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

