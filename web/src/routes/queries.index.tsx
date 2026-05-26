import { createFileRoute, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import {
  Database,
  Search,
  AlertCircle,
  Save,
  Trash2,
  ChevronDown,
  ChevronRight,
} from 'lucide-react'
import {
  api,
  type CreateQueryGroupBody,
  type QueryGroup,
  type QueryGroupKind,
  type QueryListFilters,
  type QueryRow,
} from '@/lib/api'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Sheet, SheetContent, SheetHeader, SheetTitle } from '@/components/ui/sheet'

type View = 'queries' | 'groups'

type QueriesSearch = {
  view?: View
  warehouse?: string
  user?: string
  role?: string
  status?: string
  type?: string
  has_remote_spill?: boolean
  has_queueing?: boolean
  search?: string
  // Structural filters (sqlglot-extracted; null when query_text is redacted)
  min_joins?: number
  max_joins?: number
  min_tables?: number
  max_tables?: number
  min_ctes?: number
  max_ctes?: number
  min_subqueries?: number
  max_subqueries?: number
  min_where_blocks?: number
  max_where_blocks?: number
  min_where_predicates?: number
  max_where_predicates?: number
  // Semantic predicates (Phase 2) — comma-separated table / column names.
  // Names are uppercased server-side; the input is case-insensitive.
  // ``_include`` = "query must touch ALL"; ``_exclude`` = "query must touch NONE".
  referenced_tables_include?: string
  referenced_tables_exclude?: string
  where_columns_include?: string
  where_columns_exclude?: string
  detail?: string                // open detail side-sheet for this query_id
  family?: string                // open queries filtered to this hash
  group?: number                 // open detail side-sheet for this group_id
}

export const Route = createFileRoute('/queries/')({
  validateSearch: (s: Record<string, unknown>): QueriesSearch => {
    const view = s.view === 'groups' ? 'groups' : 'queries'
    const groupRaw = s.group
    const group =
      typeof groupRaw === 'number'
        ? groupRaw
        : typeof groupRaw === 'string' && /^\d+$/.test(groupRaw)
        ? Number(groupRaw)
        : undefined
    const numParam = (v: unknown): number | undefined => {
      if (typeof v === 'number' && !Number.isNaN(v)) return v
      if (typeof v === 'string' && /^-?\d+$/.test(v)) return Number(v)
      return undefined
    }
    return {
      view,
      warehouse: typeof s.warehouse === 'string' ? s.warehouse : undefined,
      user: typeof s.user === 'string' ? s.user : undefined,
      role: typeof s.role === 'string' ? s.role : undefined,
      status: typeof s.status === 'string' ? s.status : undefined,
      type: typeof s.type === 'string' ? s.type : undefined,
      has_remote_spill: s.has_remote_spill === 'true' ? true : undefined,
      has_queueing: s.has_queueing === 'true' ? true : undefined,
      search: typeof s.search === 'string' ? s.search : undefined,
      min_joins: numParam(s.min_joins),
      max_joins: numParam(s.max_joins),
      min_tables: numParam(s.min_tables),
      max_tables: numParam(s.max_tables),
      min_ctes: numParam(s.min_ctes),
      max_ctes: numParam(s.max_ctes),
      min_subqueries: numParam(s.min_subqueries),
      max_subqueries: numParam(s.max_subqueries),
      min_where_blocks: numParam(s.min_where_blocks),
      max_where_blocks: numParam(s.max_where_blocks),
      min_where_predicates: numParam(s.min_where_predicates),
      max_where_predicates: numParam(s.max_where_predicates),
      referenced_tables_include:
        typeof s.referenced_tables_include === 'string' ? s.referenced_tables_include : undefined,
      referenced_tables_exclude:
        typeof s.referenced_tables_exclude === 'string' ? s.referenced_tables_exclude : undefined,
      where_columns_include:
        typeof s.where_columns_include === 'string' ? s.where_columns_include : undefined,
      where_columns_exclude:
        typeof s.where_columns_exclude === 'string' ? s.where_columns_exclude : undefined,
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
  const [structuralOpen, setStructuralOpen] = useState(false)
  const [semanticOpen, setSemanticOpen] = useState(false)

  const facets = useQuery({
    queryKey: ['query-facets'],
    queryFn: () => api.queryFacets(30),
  })

  const filters: QueryListFilters = useMemo(
    () => ({
      warehouse: search.warehouse,
      user: search.user,
      role: search.role,
      status: search.status,
      query_type: search.type,
      has_remote_spill: search.has_remote_spill,
      has_queueing: search.has_queueing,
      search: search.search,
      parameterized_hash: search.family,
      min_joins: search.min_joins,
      max_joins: search.max_joins,
      min_tables: search.min_tables,
      max_tables: search.max_tables,
      min_ctes: search.min_ctes,
      max_ctes: search.max_ctes,
      min_subqueries: search.min_subqueries,
      max_subqueries: search.max_subqueries,
      min_where_blocks: search.min_where_blocks,
      max_where_blocks: search.max_where_blocks,
      min_where_predicates: search.min_where_predicates,
      max_where_predicates: search.max_where_predicates,
      referenced_tables_include: search.referenced_tables_include,
      referenced_tables_exclude: search.referenced_tables_exclude,
      where_columns_include: search.where_columns_include,
      where_columns_exclude: search.where_columns_exclude,
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

  const groups = useQuery({
    queryKey: ['query-groups'],
    queryFn: () => api.listQueryGroups(),
    enabled: search.view === 'groups',
  })

  const hasStructuralFilters =
    search.min_joins != null || search.max_joins != null ||
    search.min_tables != null || search.max_tables != null ||
    search.min_ctes != null || search.max_ctes != null ||
    search.min_subqueries != null || search.max_subqueries != null ||
    search.min_where_blocks != null || search.max_where_blocks != null ||
    search.min_where_predicates != null || search.max_where_predicates != null

  const hasSemanticFilters =
    !!search.referenced_tables_include ||
    !!search.referenced_tables_exclude ||
    !!search.where_columns_include ||
    !!search.where_columns_exclude

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
    !!search.warehouse || !!search.user || !!search.role || !!search.status ||
    !!search.type || !!search.has_remote_spill || !!search.has_queueing ||
    !!search.search || !!search.family || hasStructuralFilters || hasSemanticFilters

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
                label="role"
                value={search.role}
                options={facets.data?.roles ?? []}
                onChange={(v) => setSearchParam('role', v)}
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

            {/* Structural attributes (collapsible — sqlglot-derived counts) */}
            <div className="border-t pt-3">
              <button
                onClick={() => setStructuralOpen((v) => !v)}
                className="flex items-center gap-1 text-xs uppercase text-muted-foreground hover:text-foreground"
              >
                {structuralOpen ? (
                  <ChevronDown className="h-3.5 w-3.5" />
                ) : (
                  <ChevronRight className="h-3.5 w-3.5" />
                )}
                Structural attributes
                {hasStructuralFilters && (
                  <Badge variant="secondary" className="ml-1 text-[10px]">
                    active
                  </Badge>
                )}
                <span className="ml-2 normal-case text-[11px] text-muted-foreground/80">
                  (requires <code className="font-mono">GOVERNANCE_VIEWER</code> for
                  query-text visibility)
                </span>
              </button>
              {structuralOpen && (
                <div className="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  <RangeFilter
                    label="Joins"
                    min={search.min_joins}
                    max={search.max_joins}
                    onMin={(v) => setSearchParam('min_joins', v)}
                    onMax={(v) => setSearchParam('max_joins', v)}
                  />
                  <RangeFilter
                    label="Tables referenced (distinct)"
                    min={search.min_tables}
                    max={search.max_tables}
                    onMin={(v) => setSearchParam('min_tables', v)}
                    onMax={(v) => setSearchParam('max_tables', v)}
                  />
                  <RangeFilter
                    label="CTEs"
                    min={search.min_ctes}
                    max={search.max_ctes}
                    onMin={(v) => setSearchParam('min_ctes', v)}
                    onMax={(v) => setSearchParam('max_ctes', v)}
                  />
                  <RangeFilter
                    label="Subqueries"
                    min={search.min_subqueries}
                    max={search.max_subqueries}
                    onMin={(v) => setSearchParam('min_subqueries', v)}
                    onMax={(v) => setSearchParam('max_subqueries', v)}
                  />
                  <RangeFilter
                    label="WHERE blocks"
                    min={search.min_where_blocks}
                    max={search.max_where_blocks}
                    onMin={(v) => setSearchParam('min_where_blocks', v)}
                    onMax={(v) => setSearchParam('max_where_blocks', v)}
                  />
                  <RangeFilter
                    label="WHERE predicates (leaves)"
                    min={search.min_where_predicates}
                    max={search.max_where_predicates}
                    onMin={(v) => setSearchParam('min_where_predicates', v)}
                    onMax={(v) => setSearchParam('max_where_predicates', v)}
                  />
                </div>
              )}
            </div>

            {/* Semantic predicates (Phase 2) — tables read + columns filtered */}
            <div className="border-t pt-3">
              <button
                onClick={() => setSemanticOpen((v) => !v)}
                className="flex items-center gap-1 text-xs uppercase text-muted-foreground hover:text-foreground"
              >
                {semanticOpen ? (
                  <ChevronDown className="h-3.5 w-3.5" />
                ) : (
                  <ChevronRight className="h-3.5 w-3.5" />
                )}
                Semantic filters
                {hasSemanticFilters && (
                  <Badge variant="secondary" className="ml-1 text-[10px]">
                    active
                  </Badge>
                )}
                <span className="ml-2 normal-case text-[11px] text-muted-foreground/80">
                  comma-separated names; case-insensitive; tables match both short
                  and <code className="font-mono">SCHEMA.NAME</code> forms
                </span>
              </button>
              {semanticOpen && (
                <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <TokenInput
                    label="Tables referenced — include (AND)"
                    value={search.referenced_tables_include}
                    options={facets.data?.referenced_tables ?? []}
                    placeholder="e.g. BUSINESS.SALES_OUTCOME, ORDERS"
                    onChange={(v) => setSearchParam('referenced_tables_include', v)}
                  />
                  <TokenInput
                    label="Tables referenced — exclude (none)"
                    value={search.referenced_tables_exclude}
                    options={facets.data?.referenced_tables ?? []}
                    placeholder="e.g. STAGING.RAW_EVENTS"
                    onChange={(v) => setSearchParam('referenced_tables_exclude', v)}
                  />
                  <TokenInput
                    label="WHERE columns — include (AND)"
                    value={search.where_columns_include}
                    options={facets.data?.where_columns ?? []}
                    placeholder="e.g. CLOSE_TIMESTAMP"
                    onChange={(v) => setSearchParam('where_columns_include', v)}
                  />
                  <TokenInput
                    label="WHERE columns — exclude (none)"
                    value={search.where_columns_exclude}
                    options={facets.data?.where_columns ?? []}
                    placeholder="e.g. STATUS"
                    onChange={(v) => setSearchParam('where_columns_exclude', v)}
                  />
                </div>
              )}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Body */}
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
            role: search.role,
            query_type: search.type,
            execution_status: search.status,
            query_parameterized_hash: search.family,
            has_remote_spill: search.has_remote_spill,
            has_queueing: search.has_queueing,
            search: search.search,
            min_joins: search.min_joins,
            max_joins: search.max_joins,
            min_tables: search.min_tables,
            max_tables: search.max_tables,
            min_ctes: search.min_ctes,
            max_ctes: search.max_ctes,
            min_subqueries: search.min_subqueries,
            max_subqueries: search.max_subqueries,
            min_where_blocks: search.min_where_blocks,
            max_where_blocks: search.max_where_blocks,
            min_where_predicates: search.min_where_predicates,
            max_where_predicates: search.max_where_predicates,
            referenced_tables_include: search.referenced_tables_include,
            referenced_tables_exclude: search.referenced_tables_exclude,
            where_columns_include: search.where_columns_include,
            where_columns_exclude: search.where_columns_exclude,
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

function RangeFilter({
  label,
  min,
  max,
  onMin,
  onMax,
}: {
  label: string
  min: number | undefined
  max: number | undefined
  onMin: (v: number | undefined) => void
  onMax: (v: number | undefined) => void
}) {
  function parseInput(s: string): number | undefined {
    const t = s.trim()
    if (!t) return undefined
    const n = Number(t)
    return Number.isFinite(n) && n >= 0 ? Math.floor(n) : undefined
  }
  return (
    <div className="flex flex-col gap-1">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="flex items-center gap-1.5">
        <input
          type="number"
          min={0}
          placeholder="min"
          className="w-20 rounded-md border bg-background px-2 py-1 text-sm"
          value={min ?? ''}
          onChange={(e) => onMin(parseInput(e.target.value))}
        />
        <span className="text-xs text-muted-foreground">–</span>
        <input
          type="number"
          min={0}
          placeholder="max"
          className="w-20 rounded-md border bg-background px-2 py-1 text-sm"
          value={max ?? ''}
          onChange={(e) => onMax(parseInput(e.target.value))}
        />
      </div>
    </div>
  )
}

// Comma-separated multi-value input with datalist autocomplete.  Used for
// the Phase 2 semantic filters (tables / WHERE columns).  Lightweight on
// purpose — typing comma-separated values mirrors how the URL filter looks,
// and the datalist gives suggestion-driven autocomplete without pulling in
// a full combobox component.  Token-pill UX is a future polish pass.
function TokenInput({
  label,
  value,
  options,
  placeholder,
  onChange,
}: {
  label: string
  value: string | undefined
  options: string[]
  placeholder?: string
  onChange: (v: string | undefined) => void
}) {
  // The datalist id has to be unique per input on the page; derive it
  // from the label (collisions unlikely given there are only 4).
  const listId = `tokeninput-${label.replace(/[^a-z0-9]+/gi, '-').toLowerCase()}`
  return (
    <div className="flex flex-col gap-1">
      <label className="text-[11px] uppercase tracking-wide text-muted-foreground">
        {label}
      </label>
      <input
        type="text"
        list={listId}
        placeholder={placeholder ?? 'comma-separated…'}
        className="rounded-md border bg-background px-2 py-1 text-sm"
        value={value ?? ''}
        onChange={(e) => {
          const v = e.target.value
          // Trim outer whitespace but keep commas as-is while the user types.
          // Empty string clears the filter entirely.
          onChange(v.trim() ? v : undefined)
        }}
      />
      <datalist id={listId}>
        {options.slice(0, 200).map((o) => (
          <option key={o} value={o} />
        ))}
      </datalist>
    </div>
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
                    <th className="px-4 py-2 whitespace-nowrap">Parameterized hash</th>
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
              <h3 className="mb-2 text-xs uppercase text-muted-foreground">
                Parameterized hash
              </h3>
              <code className="block break-all font-mono text-xs">
                {d.query_parameterized_hash}
              </code>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardContent className="p-4 text-sm">
            <h3 className="mb-3 text-xs uppercase text-muted-foreground">
              Structural attributes
              <span className="ml-2 normal-case text-[11px] text-muted-foreground/80">
                (sqlglot-extracted)
              </span>
            </h3>
            {d.sql_features_parse_error ? (
              <div className="text-xs text-muted-foreground">
                Not available: <code className="font-mono">{d.sql_features_parse_error}</code>
                {d.sql_features_parse_error === 'redacted' && (
                  <span className="ml-1">
                    — grant <code className="font-mono">GOVERNANCE_VIEWER</code> for visibility.
                  </span>
                )}
              </div>
            ) : (
              <>
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Joins" value={fmtCount(d.joins_count)} />
                  <Field label="Tables referenced" value={fmtCount(d.tables_referenced_count)} />
                  <Field label="CTEs" value={fmtCount(d.ctes_count)} />
                  <Field label="Subqueries" value={fmtCount(d.subqueries_count)} />
                  <Field label="WHERE blocks" value={fmtCount(d.where_block_count)} />
                  <Field label="WHERE predicates" value={fmtCount(d.where_predicate_count)} />
                </div>
                {/* Semantic lists (Phase 2).  Empty arrays render as a
                    placeholder so the user can tell "parsed but none" from
                    "not yet extracted" (which would be the parse_error path). */}
                <div className="mt-4 space-y-3 border-t pt-3 text-xs">
                  <ChipList
                    label="Tables referenced"
                    items={d.referenced_tables ?? []}
                    note="both fully-qualified and short forms shown"
                  />
                  <ChipList
                    label="Columns in WHERE"
                    items={d.where_columns ?? []}
                    note="any column appearing anywhere in any WHERE clause"
                  />
                </div>
              </>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  )
}

function ChipList({
  label,
  items,
  note,
}: {
  label: string
  items: string[]
  note?: string
}) {
  return (
    <div>
      <div className="mb-1 flex items-center gap-2">
        <span className="uppercase tracking-wide text-muted-foreground">{label}</span>
        {note && (
          <span className="text-[10px] normal-case text-muted-foreground/70">{note}</span>
        )}
      </div>
      {items.length === 0 ? (
        <span className="text-muted-foreground">(none)</span>
      ) : (
        <div className="flex flex-wrap gap-1">
          {items.map((it) => (
            <code
              key={it}
              className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]"
            >
              {it}
            </code>
          ))}
        </div>
      )}
    </div>
  )
}

function fmtCount(n: number | null | undefined): string {
  return n == null ? '—' : String(n)
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
  role?: string
  query_type?: string
  execution_status?: string
  query_parameterized_hash?: string
  has_remote_spill?: boolean
  has_queueing?: boolean
  search?: string
  min_joins?: number
  max_joins?: number
  min_tables?: number
  max_tables?: number
  min_ctes?: number
  max_ctes?: number
  min_subqueries?: number
  max_subqueries?: number
  min_where_blocks?: number
  max_where_blocks?: number
  min_where_predicates?: number
  max_where_predicates?: number
  // Semantic (Phase 2) — comma-separated table / column names
  referenced_tables_include?: string
  referenced_tables_exclude?: string
  where_columns_include?: string
  where_columns_exclude?: string
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
        // Map the dialog's filter shape to the API's CreateQueryGroupBody.
        // The query-string filters use `query_type` / `status` / `family`;
        // the API uses `query_type` / `execution_status` /
        // `query_parameterized_hash`.  Already aligned in `currentFilters`.
        warehouse_name: currentFilters.warehouse,
        user_name: currentFilters.user,
        role_name: currentFilters.role,
        query_type: currentFilters.query_type,
        execution_status: currentFilters.execution_status,
        query_parameterized_hash: currentFilters.query_parameterized_hash,
        has_remote_spill: currentFilters.has_remote_spill,
        has_queueing: currentFilters.has_queueing,
        search: currentFilters.search,
        min_joins: currentFilters.min_joins,
        max_joins: currentFilters.max_joins,
        min_tables: currentFilters.min_tables,
        max_tables: currentFilters.max_tables,
        min_ctes: currentFilters.min_ctes,
        max_ctes: currentFilters.max_ctes,
        min_subqueries: currentFilters.min_subqueries,
        max_subqueries: currentFilters.max_subqueries,
        min_where_blocks: currentFilters.min_where_blocks,
        max_where_blocks: currentFilters.max_where_blocks,
        min_where_predicates: currentFilters.min_where_predicates,
        max_where_predicates: currentFilters.max_where_predicates,
        referenced_tables_include: currentFilters.referenced_tables_include,
        referenced_tables_exclude: currentFilters.referenced_tables_exclude,
        where_columns_include: currentFilters.where_columns_include,
        where_columns_exclude: currentFilters.where_columns_exclude,
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
  if (currentFilters.role) filterChips.push(`role=${currentFilters.role}`)
  if (currentFilters.query_type) filterChips.push(`type=${currentFilters.query_type}`)
  if (currentFilters.execution_status) filterChips.push(`status=${currentFilters.execution_status}`)
  if (currentFilters.query_parameterized_hash)
    filterChips.push(`hash=${currentFilters.query_parameterized_hash.slice(0, 12)}…`)
  if (currentFilters.has_remote_spill) filterChips.push('has_remote_spill')
  if (currentFilters.has_queueing) filterChips.push('has_queueing')
  if (currentFilters.search) filterChips.push(`search="${currentFilters.search}"`)
  // Structural
  const range = (label: string, lo: number | undefined, hi: number | undefined) => {
    if (lo == null && hi == null) return
    filterChips.push(`${label}=${lo ?? '*'}..${hi ?? '*'}`)
  }
  range('joins', currentFilters.min_joins, currentFilters.max_joins)
  range('tables', currentFilters.min_tables, currentFilters.max_tables)
  range('ctes', currentFilters.min_ctes, currentFilters.max_ctes)
  range('subqueries', currentFilters.min_subqueries, currentFilters.max_subqueries)
  range('where_blocks', currentFilters.min_where_blocks, currentFilters.max_where_blocks)
  range('where_predicates', currentFilters.min_where_predicates, currentFilters.max_where_predicates)
  // Semantic chips (Phase 2)
  if (currentFilters.referenced_tables_include)
    filterChips.push(`tables⊇{${currentFilters.referenced_tables_include}}`)
  if (currentFilters.referenced_tables_exclude)
    filterChips.push(`tables∌{${currentFilters.referenced_tables_exclude}}`)
  if (currentFilters.where_columns_include)
    filterChips.push(`where⊇{${currentFilters.where_columns_include}}`)
  if (currentFilters.where_columns_exclude)
    filterChips.push(`where∌{${currentFilters.where_columns_exclude}}`)

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
  if (spec.role_name?.length) chips.push({ label: 'role', value: spec.role_name.join(', ') })
  if (spec.query_type?.length) chips.push({ label: 'type', value: spec.query_type.join(', ') })
  if (spec.execution_status?.length) chips.push({ label: 'status', value: spec.execution_status.join(', ') })
  if (spec.query_parameterized_hash?.length)
    chips.push({ label: 'hash', value: spec.query_parameterized_hash.join(', ').slice(0, 30) + '…' })
  if (spec.has_remote_spill === true) chips.push({ label: 'has_remote_spill', value: 'true' })
  if (spec.has_local_spill === true) chips.push({ label: 'has_local_spill', value: 'true' })
  if (spec.has_queueing === true) chips.push({ label: 'has_queueing', value: 'true' })
  if (spec.min_elapsed_ms != null) chips.push({ label: 'min_elapsed_ms', value: String(spec.min_elapsed_ms) })
  if (spec.max_elapsed_ms != null) chips.push({ label: 'max_elapsed_ms', value: String(spec.max_elapsed_ms) })
  if (spec.search) chips.push({ label: 'search', value: `"${spec.search}"` })

  // Structural range chips
  const range = (label: string, lo: number | null | undefined, hi: number | null | undefined) => {
    if (lo == null && hi == null) return
    chips.push({ label, value: `${lo ?? '*'}..${hi ?? '*'}` })
  }
  range('joins', spec.min_joins, spec.max_joins)
  range('tables', spec.min_tables, spec.max_tables)
  range('ctes', spec.min_ctes, spec.max_ctes)
  range('subqueries', spec.min_subqueries, spec.max_subqueries)
  range('where_blocks', spec.min_where_blocks, spec.max_where_blocks)
  range('where_predicates', spec.min_where_predicates, spec.max_where_predicates)

  // Semantic chips (Phase 2).  The spec stores these as ``list[str]``, so
  // render the values joined by ``,`` for compactness.
  if (spec.referenced_tables_include?.length)
    chips.push({ label: 'tables⊇', value: spec.referenced_tables_include.join(',') })
  if (spec.referenced_tables_exclude?.length)
    chips.push({ label: 'tables∌', value: spec.referenced_tables_exclude.join(',') })
  if (spec.where_columns_include?.length)
    chips.push({ label: 'where⊇', value: spec.where_columns_include.join(',') })
  if (spec.where_columns_exclude?.length)
    chips.push({ label: 'where∌', value: spec.where_columns_exclude.join(',') })

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

