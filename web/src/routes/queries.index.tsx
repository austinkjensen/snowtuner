import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Database, Search, AlertCircle, Save, Trash2 } from 'lucide-react'
import {
  api,
  type CreateQueryGroupBody,
  type QueryGroup,
  type QueryGroupKind,
  type QueryListFilters,
  type QueryRow,
  type QueryFamily,
} from '@/lib/api'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'

type View = 'queries' | 'families' | 'groups'

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
  group?: number                 // open detail side-sheet for this group_id
}

export const Route = createFileRoute('/queries/')({
  validateSearch: (s: Record<string, unknown>): QueriesSearch => {
    const view =
      s.view === 'families' ? 'families' : s.view === 'groups' ? 'groups' : 'queries'
    const groupRaw = s.group
    const group =
      typeof groupRaw === 'number'
        ? groupRaw
        : typeof groupRaw === 'string' && /^\d+$/.test(groupRaw)
        ? Number(groupRaw)
        : undefined
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
      group,
    }
  },
  component: QueriesExplorer,
})

const PAGE_SIZE = 50

function QueriesExplorer() {
  const search = Route.useSearch()
  const navigate = useNavigate({ from: '/queries/' })
  const qc = useQueryClient()
  const [offset, setOffset] = useState(0)
  const [showSaveDialog, setShowSaveDialog] = useState(false)

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
    enabled: search.view === 'queries',
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

  const groups = useQuery({
    queryKey: ['query-groups'],
    queryFn: () => api.listQueryGroups(),
    enabled: search.view === 'groups',
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

  function openGroup(id: number) {
    navigate({ search: { ...search, group: id } })
  }

  function closeGroupDetail() {
    const { group, ...rest } = search
    void group
    navigate({ search: rest })
  }

  const hasActiveFilters =
    !!search.warehouse || !!search.user || !!search.status || !!search.type ||
    !!search.has_remote_spill || !!search.has_queueing || !!search.search ||
    !!search.family

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      {/* Header */}
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Database className="h-6 w-6 text-primary/80" />
          <h1 className="text-2xl font-semibold">Queries</h1>
          <span className="text-sm text-muted-foreground">
            Explore ingested query history; group by family; save reusable
            query groups
          </span>
        </div>
        {search.view !== 'groups' && (
          <Button
            variant="default"
            className="gap-1.5"
            onClick={() => setShowSaveDialog(true)}
            disabled={!hasActiveFilters}
            title={
              hasActiveFilters
                ? 'Save the current filter as a reusable group'
                : 'Apply at least one filter first'
            }
          >
            <Save className="h-4 w-4" />
            Save as group
          </Button>
        )}
      </div>

      {/* View toggle */}
      <div className="mb-4 inline-flex rounded-md border bg-muted/30 p-0.5">
        <button
          onClick={() => navigate({ search: { ...search, view: 'queries' } })}
          className={`rounded px-3 py-1.5 text-sm ${search.view === 'queries' ? 'bg-background shadow-sm' : 'text-muted-foreground'}`}
        >
          Queries
        </button>
        <button
          onClick={() => navigate({ search: { ...search, view: 'families' } })}
          className={`rounded px-3 py-1.5 text-sm ${search.view === 'families' ? 'bg-background shadow-sm' : 'text-muted-foreground'}`}
        >
          Families
        </button>
        <button
          onClick={() => navigate({ search: { ...search, view: 'groups' } })}
          className={`rounded px-3 py-1.5 text-sm ${search.view === 'groups' ? 'bg-background shadow-sm' : 'text-muted-foreground'}`}
        >
          Groups
        </button>
      </div>

      {/* Filter chips (hidden in groups view — filters don't apply there) */}
      {search.view !== 'groups' && (
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
      )}

      {/* Body */}
      {search.view === 'families' && (
        <FamiliesTable
          rows={families.data ?? []}
          loading={families.isLoading}
          onDrill={drillIntoFamily}
        />
      )}
      {search.view === 'groups' && (
        <GroupsTable
          rows={groups.data ?? []}
          loading={groups.isLoading}
          onOpen={openGroup}
        />
      )}
      {search.view === 'queries' && (
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

      {/* Save-as-group dialog */}
      {showSaveDialog && (
        <SaveAsGroupDialog
          currentFilters={{
            warehouse: search.warehouse,
            user: search.user,
            query_type: search.type,
            execution_status: search.status,
            query_parameterized_hash: search.family,
            has_remote_spill: search.has_remote_spill,
            has_queueing: search.has_queueing,
            search: search.search,
          }}
          onClose={() => setShowSaveDialog(false)}
          onCreated={(g) => {
            setShowSaveDialog(false)
            qc.invalidateQueries({ queryKey: ['query-groups'] })
            // Surface the created group by jumping to its detail panel
            navigate({ search: { ...search, view: 'groups', group: g.id } })
          }}
        />
      )}

      {/* Query detail sheet */}
      <Sheet open={!!search.detail} onOpenChange={(open) => !open && closeDetail()}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
          {search.detail && <QueryDetailPanel queryId={search.detail} />}
        </SheetContent>
      </Sheet>

      {/* Group detail sheet */}
      <Sheet open={!!search.group} onOpenChange={(open) => !open && closeGroupDetail()}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-3xl">
          {search.group && (
            <GroupDetailPanel
              groupId={search.group}
              onDelete={() => {
                qc.invalidateQueries({ queryKey: ['query-groups'] })
                closeGroupDetail()
              }}
              onOpenQuery={openDetail}
            />
          )}
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
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2">Query</th>
                    <th className="px-4 py-2 whitespace-nowrap">Warehouse</th>
                    <th className="px-4 py-2 whitespace-nowrap">User</th>
                    <th className="px-4 py-2 whitespace-nowrap">Status</th>
                    <th className="px-4 py-2 text-right whitespace-nowrap">Elapsed</th>
                    <th className="px-4 py-2 text-right whitespace-nowrap">Scanned</th>
                    <th className="px-4 py-2 text-right whitespace-nowrap">Spill</th>
                    <th className="px-4 py-2 whitespace-nowrap">Started</th>
                    <th className="px-4 py-2 whitespace-nowrap">Family</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr
                      key={r.query_id}
                      className="cursor-pointer border-b hover:bg-muted/30"
                      onClick={() => onOpenDetail(r.query_id)}
                    >
                      <td className="px-4 py-2 font-mono text-xs">
                        <div className="max-w-xs truncate" title={r.query_text_preview}>
                          {r.query_text_preview}
                        </div>
                      </td>
                      <td className="px-4 py-2 max-w-[14rem] truncate" title={r.warehouse_name ?? ''}>
                        {r.warehouse_name ?? '—'}
                      </td>
                      <td className="px-4 py-2 whitespace-nowrap">{r.user_name ?? '—'}</td>
                      <td className="px-4 py-2 whitespace-nowrap">
                        {r.execution_status === 'SUCCESS' ? (
                          <Badge variant="secondary">{r.execution_status}</Badge>
                        ) : (
                          <Badge variant="destructive">{r.execution_status ?? '—'}</Badge>
                        )}
                      </td>
                      <td className="px-4 py-2 text-right font-mono whitespace-nowrap">
                        {fmtMs(r.total_elapsed_ms)}
                      </td>
                      <td className="px-4 py-2 text-right font-mono text-xs whitespace-nowrap">
                        {fmtBytes(r.bytes_scanned)}
                      </td>
                      <td className="px-4 py-2 text-right whitespace-nowrap">
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
                      <td className="px-4 py-2 text-xs text-muted-foreground whitespace-nowrap">
                        {fmtDt(r.start_time)}
                      </td>
                      <td className="px-4 py-2 whitespace-nowrap">
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
            </div>
            {/* Pagination — outside the horizontal scroll so it's always visible */}
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
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2">Family / sample SQL</th>
                  <th className="px-4 py-2 text-right whitespace-nowrap">Count</th>
                  <th className="px-4 py-2 text-right whitespace-nowrap">Mean elapsed</th>
                  <th className="px-4 py-2 text-right whitespace-nowrap">p95 elapsed</th>
                  <th className="px-4 py-2 text-right whitespace-nowrap">Total elapsed</th>
                  <th className="px-4 py-2 text-right whitespace-nowrap">Spilled</th>
                  <th className="px-4 py-2 text-right whitespace-nowrap">Failed</th>
                  <th className="px-4 py-2 whitespace-nowrap">Last seen</th>
                  <th className="px-4 py-2 whitespace-nowrap"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((f) => (
                  <tr key={f.query_parameterized_hash} className="border-b hover:bg-muted/30">
                    <td className="px-4 py-2">
                      <div className="max-w-sm">
                        <div className="font-mono text-xs text-muted-foreground truncate">
                          {f.query_parameterized_hash.slice(0, 12)}…
                        </div>
                        <div
                          className="truncate font-mono text-xs"
                          title={f.representative_sql}
                        >
                          {f.representative_sql}
                        </div>
                      </div>
                    </td>
                    <td className="px-4 py-2 text-right whitespace-nowrap">{f.occurrence_count}</td>
                    <td className="px-4 py-2 text-right font-mono whitespace-nowrap">
                      {fmtMs(f.mean_elapsed_ms)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono whitespace-nowrap">
                      {fmtMs(f.p95_elapsed_ms)}
                    </td>
                    <td className="px-4 py-2 text-right font-mono whitespace-nowrap">
                      {fmtMs(f.total_elapsed_ms)}
                    </td>
                    <td className="px-4 py-2 text-right whitespace-nowrap">
                      {f.n_spill_remote > 0 ? (
                        <span className="text-destructive">{f.n_spill_remote}</span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-right whitespace-nowrap">
                      {f.n_failed > 0 ? (
                        <span className="text-destructive">{f.n_failed}</span>
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground whitespace-nowrap">
                      {fmtDt(f.last_seen)}
                    </td>
                    <td className="px-4 py-2 whitespace-nowrap">
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
          </div>
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
  // `min-w-0` lets the grid cell shrink below its content's intrinsic width
  // (default grid items have `min-width: auto`, which means content can push
  // the cell wider than its column allocation — that's what causes long
  // warehouse names to overlap the neighbouring cell).
  // `break-all` lets long identifiers (warehouse names, query IDs, etc.) wrap
  // at any character instead of overflowing.
  return (
    <div className="min-w-0">
      <div className="text-xs uppercase text-muted-foreground">{label}</div>
      <div className="font-mono break-all">{value}</div>
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

// ── Groups view ──────────────────────────────────────────────────────

function GroupsTable({
  rows,
  loading,
  onOpen,
}: {
  rows: QueryGroup[]
  loading: boolean
  onOpen: (id: number) => void
}) {
  return (
    <Card>
      <CardContent className="p-0">
        {loading && <div className="p-4 text-sm text-muted-foreground">Loading…</div>}
        {!loading && rows.length === 0 && (
          <div className="p-8 text-center text-sm text-muted-foreground">
            No saved groups yet. Apply some filters on the Queries view and click{' '}
            <span className="font-medium">Save as group</span> to create one.
          </div>
        )}
        {rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-right whitespace-nowrap">#</th>
                  <th className="px-4 py-2">Name</th>
                  <th className="px-4 py-2 whitespace-nowrap">Kind</th>
                  <th className="px-4 py-2 text-right whitespace-nowrap">Members</th>
                  <th className="px-4 py-2 whitespace-nowrap">Created</th>
                  <th className="px-4 py-2 whitespace-nowrap"></th>
                </tr>
              </thead>
              <tbody>
                {rows.map((g) => (
                  <tr
                    key={g.id}
                    className="cursor-pointer border-b hover:bg-muted/30"
                    onClick={() => onOpen(g.id)}
                  >
                    <td className="px-4 py-2 text-right font-mono text-xs">{g.id}</td>
                    <td className="px-4 py-2">
                      <div className="font-medium">{g.name}</div>
                      {g.description && (
                        <div className="text-xs text-muted-foreground truncate max-w-xl">
                          {g.description}
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-2 whitespace-nowrap">
                      <Badge variant={g.kind === 'static' ? 'outline' : 'secondary'}>
                        {g.kind}
                      </Badge>
                    </td>
                    <td className="px-4 py-2 text-right whitespace-nowrap">
                      {g.member_count ?? '—'}
                    </td>
                    <td className="px-4 py-2 text-xs text-muted-foreground whitespace-nowrap">
                      {fmtDt(g.created_at)}
                    </td>
                    <td className="px-4 py-2 whitespace-nowrap">
                      <span className="text-xs text-primary">View →</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

// ── Save-as-group dialog ─────────────────────────────────────────────

type CurrentFilters = {
  warehouse?: string
  user?: string
  query_type?: string
  execution_status?: string
  query_parameterized_hash?: string
  has_remote_spill?: boolean
  has_queueing?: boolean
  search?: string
}

function SaveAsGroupDialog({
  currentFilters,
  onClose,
  onCreated,
}: {
  currentFilters: CurrentFilters
  onClose: () => void
  onCreated: (g: QueryGroup) => void
}) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [kind, setKind] = useState<QueryGroupKind>('dynamic')
  const [error, setError] = useState<string | null>(null)

  const create = useMutation({
    mutationFn: () => {
      const body: CreateQueryGroupBody = {
        name: name.trim(),
        description: description.trim() || null,
        kind,
        ...currentFilters,
      }
      return api.createQueryGroup(body)
    },
    onSuccess: onCreated,
    onError: (e: Error) => setError(e.message),
  })

  // Show the user what their group will contain — filter chips preview
  const filterChips: string[] = []
  if (currentFilters.warehouse) filterChips.push(`warehouse=${currentFilters.warehouse}`)
  if (currentFilters.user) filterChips.push(`user=${currentFilters.user}`)
  if (currentFilters.query_type) filterChips.push(`type=${currentFilters.query_type}`)
  if (currentFilters.execution_status) filterChips.push(`status=${currentFilters.execution_status}`)
  if (currentFilters.query_parameterized_hash)
    filterChips.push(`family=${currentFilters.query_parameterized_hash.slice(0, 12)}…`)
  if (currentFilters.has_remote_spill) filterChips.push('has_remote_spill')
  if (currentFilters.has_queueing) filterChips.push('has_queueing')
  if (currentFilters.search) filterChips.push(`search="${currentFilters.search}"`)

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={onClose}
    >
      <Card className="w-full max-w-lg" onClick={(e) => e.stopPropagation()}>
        <CardContent className="space-y-4 p-6">
          <div>
            <h2 className="text-lg font-semibold">Save current filter as group</h2>
            <p className="mt-1 text-xs text-muted-foreground">
              Groups are reusable named subsets of your query history. You can later
              feed them into experiments or revisit them under the Groups tab.
            </p>
          </div>

          <div>
            <label className="text-sm font-medium">Name</label>
            <input
              type="text"
              autoFocus
              className="mt-1 w-full rounded-md border bg-background px-3 py-1.5 text-sm"
              placeholder="e.g. ETL slow queries"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div>
            <label className="text-sm font-medium">Description (optional)</label>
            <input
              type="text"
              className="mt-1 w-full rounded-md border bg-background px-3 py-1.5 text-sm"
              placeholder="What is this group for?"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>

          <div>
            <label className="text-sm font-medium">Kind</label>
            <div className="mt-1 flex gap-3 text-sm">
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  className="mt-1"
                  checked={kind === 'dynamic'}
                  onChange={() => setKind('dynamic')}
                />
                <span>
                  <span className="font-medium">Dynamic</span>
                  <span className="ml-1 text-xs text-muted-foreground">
                    — re-evaluated on every read; future matches are included automatically
                  </span>
                </span>
              </label>
              <label className="flex items-start gap-2">
                <input
                  type="radio"
                  className="mt-1"
                  checked={kind === 'static'}
                  onChange={() => setKind('static')}
                />
                <span>
                  <span className="font-medium">Static</span>
                  <span className="ml-1 text-xs text-muted-foreground">
                    — members frozen at save time; reproducible
                  </span>
                </span>
              </label>
            </div>
          </div>

          <div>
            <label className="text-sm font-medium">Filter</label>
            <div className="mt-1 flex flex-wrap gap-1.5">
              {filterChips.length === 0 ? (
                <span className="text-xs text-muted-foreground">
                  (no filters — the group will include every query)
                </span>
              ) : (
                filterChips.map((c, i) => (
                  <code
                    key={i}
                    className="rounded bg-muted px-1.5 py-0.5 text-xs font-mono"
                  >
                    {c}
                  </code>
                ))
              )}
            </div>
          </div>

          {error && <div className="text-sm text-destructive">{error}</div>}

          <div className="flex justify-end gap-2 pt-2">
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button
              onClick={() => create.mutate()}
              disabled={!name.trim() || create.isPending}
            >
              {create.isPending ? 'Saving…' : 'Save group'}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}

// ── Group detail side-sheet ──────────────────────────────────────────

function GroupDetailPanel({
  groupId,
  onDelete,
  onOpenQuery,
}: {
  groupId: number
  onDelete: () => void
  onOpenQuery: (queryId: string) => void
}) {
  const group = useQuery({
    queryKey: ['query-group', groupId],
    queryFn: () => api.getQueryGroup(groupId),
  })
  const members = useQuery({
    queryKey: ['query-group-members', groupId],
    queryFn: () => api.queryGroupMembers(groupId, { limit: 50 }),
  })
  const del = useMutation({
    mutationFn: () => api.deleteQueryGroup(groupId),
    onSuccess: onDelete,
  })
  const [confirmDelete, setConfirmDelete] = useState(false)

  if (group.isLoading) return <div className="p-4 text-sm">Loading…</div>
  if (group.error)
    return <div className="p-4 text-sm text-destructive">{String(group.error)}</div>
  if (!group.data) return null
  const g = group.data

  // Render filter spec as readable chips
  const spec = g.filter_spec
  const chips: { label: string; value: string }[] = []
  if (spec.warehouse_name?.length) chips.push({ label: 'warehouse', value: spec.warehouse_name.join(', ') })
  if (spec.user_name?.length) chips.push({ label: 'user', value: spec.user_name.join(', ') })
  if (spec.query_type?.length) chips.push({ label: 'type', value: spec.query_type.join(', ') })
  if (spec.execution_status?.length) chips.push({ label: 'status', value: spec.execution_status.join(', ') })
  if (spec.query_parameterized_hash?.length)
    chips.push({ label: 'family', value: spec.query_parameterized_hash.join(', ').slice(0, 30) + '…' })
  if (spec.has_remote_spill === true) chips.push({ label: 'has_remote_spill', value: 'true' })
  if (spec.has_local_spill === true) chips.push({ label: 'has_local_spill', value: 'true' })
  if (spec.has_queueing === true) chips.push({ label: 'has_queueing', value: 'true' })
  if (spec.min_elapsed_ms != null) chips.push({ label: 'min_elapsed_ms', value: String(spec.min_elapsed_ms) })
  if (spec.max_elapsed_ms != null) chips.push({ label: 'max_elapsed_ms', value: String(spec.max_elapsed_ms) })
  if (spec.search) chips.push({ label: 'search', value: `"${spec.search}"` })

  return (
    <>
      <SheetHeader>
        <SheetTitle className="flex items-center gap-2">
          {g.name}
          <Badge variant={g.kind === 'static' ? 'outline' : 'secondary'}>{g.kind}</Badge>
        </SheetTitle>
      </SheetHeader>

      <div className="mt-4 space-y-4">
        {g.description && (
          <Card>
            <CardContent className="p-4 text-sm">
              <h3 className="mb-1 text-xs uppercase text-muted-foreground">Description</h3>
              <p>{g.description}</p>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardContent className="space-y-3 p-4 text-sm">
            <div>
              <h3 className="mb-1 text-xs uppercase text-muted-foreground">Filter</h3>
              {chips.length === 0 ? (
                <span className="text-xs text-muted-foreground">
                  (no filters — every query matches)
                </span>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {chips.map((c, i) => (
                    <code
                      key={i}
                      className="rounded bg-muted px-1.5 py-0.5 text-xs font-mono"
                    >
                      {c.label}={c.value}
                    </code>
                  ))}
                </div>
              )}
            </div>
            <div className="grid grid-cols-2 gap-3 border-t pt-3 text-xs">
              <div>
                <div className="uppercase text-muted-foreground">Created</div>
                <div className="font-mono">{fmtDt(g.created_at)}</div>
              </div>
              <div>
                <div className="uppercase text-muted-foreground">Members</div>
                <div className="font-mono">{g.member_count ?? '—'}</div>
              </div>
              {g.kind === 'static' && g.snapshot_at && (
                <div className="col-span-2">
                  <div className="uppercase text-muted-foreground">Snapshot taken</div>
                  <div className="font-mono">{fmtDt(g.snapshot_at)}</div>
                </div>
              )}
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-0">
            <div className="border-b px-4 py-2 text-xs uppercase text-muted-foreground">
              Members ({members.data?.total ?? '…'})
            </div>
            {members.isLoading && (
              <div className="p-4 text-sm text-muted-foreground">Loading…</div>
            )}
            {members.data && members.data.rows.length === 0 && (
              <div className="p-4 text-sm text-muted-foreground">
                No queries currently match this group's filter.
              </div>
            )}
            {members.data && members.data.rows.length > 0 && (
              <div className="max-h-96 overflow-auto">
                <table className="w-full text-xs">
                  <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground sticky top-0">
                    <tr>
                      <th className="px-3 py-1.5">Query</th>
                      <th className="px-3 py-1.5 whitespace-nowrap">Warehouse</th>
                      <th className="px-3 py-1.5 whitespace-nowrap">User</th>
                      <th className="px-3 py-1.5 text-right whitespace-nowrap">Elapsed</th>
                    </tr>
                  </thead>
                  <tbody>
                    {members.data.rows.map((r) => (
                      <tr
                        key={r.query_id}
                        className="cursor-pointer border-b hover:bg-muted/30"
                        onClick={() => onOpenQuery(r.query_id)}
                      >
                        <td className="px-3 py-1.5 font-mono">
                          <div className="max-w-xs truncate" title={r.query_text_preview}>
                            {r.query_text_preview}
                          </div>
                        </td>
                        <td className="px-3 py-1.5 max-w-[10rem] truncate" title={r.warehouse_name ?? ''}>
                          {r.warehouse_name ?? '—'}
                        </td>
                        <td className="px-3 py-1.5 whitespace-nowrap">{r.user_name ?? '—'}</td>
                        <td className="px-3 py-1.5 text-right font-mono whitespace-nowrap">
                          {fmtMs(r.total_elapsed_ms)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="p-4">
            {!confirmDelete ? (
              <Button
                variant="destructive"
                className="w-full gap-1.5"
                onClick={() => setConfirmDelete(true)}
              >
                <Trash2 className="h-4 w-4" />
                Delete group
              </Button>
            ) : (
              <div className="space-y-2">
                <p className="text-sm">
                  Delete <span className="font-medium">{g.name}</span>? This cannot
                  be undone.
                </p>
                <div className="flex gap-2">
                  <Button
                    variant="destructive"
                    className="flex-1"
                    onClick={() => del.mutate()}
                    disabled={del.isPending}
                  >
                    {del.isPending ? 'Deleting…' : 'Confirm delete'}
                  </Button>
                  <Button
                    variant="outline"
                    className="flex-1"
                    onClick={() => setConfirmDelete(false)}
                  >
                    Cancel
                  </Button>
                </div>
                {del.error && (
                  <div className="text-xs text-destructive">{String(del.error)}</div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  )
}

