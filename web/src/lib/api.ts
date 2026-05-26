/**
 * Tiny typed API client over the snowtuner FastAPI.
 *
 * All requests go to the same origin in dev (Vite proxies /api/* → 127.0.0.1:8770)
 * and in prod (when we eventually serve the built SPA from FastAPI itself).
 */
import type { components, paths } from './api-types'

type Schemas = components['schemas']

export type Recommendation = Schemas['RecommendationOut']
export type Warehouse = Schemas['WarehouseSummaryOut']
export type Status = Schemas['StatusOut']
export type SourceFreshness = Schemas['SourceFreshnessOut']
export type RecommenderInfo = Schemas['RecommenderInfo']
export type AutonomousConfig = Schemas['AutonomousConfigOut']
export type AutonomousApplication = Schemas['AutonomousApplicationOut']
export type CredentialStatus = Schemas['CredentialStatusOut']
export type CredentialVerify = Schemas['CredentialVerifyOut']

// ── Type aliases backed by the generated OpenAPI schema ────────────
// Run `npm run gen-types` (against a running API) to refresh api-types.ts.
// CI should fail if the generated file is out of sync.  Adding a new
// endpoint or changing a Pydantic shape on the backend → regenerate → all
// the TS consumers automatically pick up the new shape (or fail to compile).

export type RecommendationStatus = Schemas['RecommendationStatus']
export type ExperimentStatus = Schemas['ExperimentStatus']
export type ExperimentKind = Schemas['ExperimentKind']

export type RecipeInfo = Schemas['RecipeInfo']
export type BenchmarkArmSpec = Schemas['BenchmarkArmSpec']
export type ProposeBenchmarkRequest = Schemas['ProposeBenchmarkRequest']
export type CostEstimate = Schemas['CostEstimate']
export type Issue = Schemas['Issue']
export type ArmConfigDelta = Schemas['WarehouseConfigDelta']
export type Arm = Schemas['Arm']
export type ProposedExperiment = Schemas['ProposedExperiment']
export type ArmObservation = Schemas['ArmObservation']
export type ExperimentReport = Schemas['ExperimentReport']
export type Experiment = Schemas['Experiment']

// ── Queries explorer (generated) ───────────────────────────────

export type QueryRow = Schemas['QueryRow']
export type QueryDetail = Schemas['QueryDetail']
export type QueryFamily = Schemas['QueryFamily']
export type QueryListResponse = Schemas['QueryListResponse']
export type QueryFilterFacets = Schemas['QueryFilterFacets']

// ── Query groups (generated) ───────────────────────────────────

export type QueryGroupKind = Schemas['QueryGroupKind']
export type QueryFilterSpec = Schemas['QueryFilterSpec']

export type QueryGroup = Schemas['QueryGroup']
export type CreateQueryGroupBody = Schemas['CreateQueryGroupRequest']

// ── Self-documentation types ──────────────────────────────────

export type CliCommand = Schemas['CliCommand']
export type CliParam = Schemas['CliParam']
export type McpToolInfo = Schemas['McpToolInfo']

export interface QueryListFilters {
  warehouse?: string         // comma-separated
  user?: string
  role?: string
  query_type?: string
  status?: string
  parameterized_hash?: string
  start_from?: string
  start_to?: string
  min_elapsed_ms?: number
  max_elapsed_ms?: number
  has_remote_spill?: boolean
  has_local_spill?: boolean
  has_queueing?: boolean
  search?: string
  // Structural filters
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
  referenced_tables_include?: string
  referenced_tables_exclude?: string
  where_columns_include?: string
  where_columns_exclude?: string
  limit?: number
  offset?: number
}

export type ExperimentRun = Schemas['ExperimentRun']

const BASE = '/api'

// ── Auth ──────────────────────────────────────────────────────
// When the API runs in SNOWTUNER_AUTH_MODE=token, every request needs an
// Authorization: Bearer <token> header.  The token lives in localStorage
// under this key; the user pastes it once via /settings or however they
// got it from `snowtuner auth show`.  In SNOWTUNER_AUTH_MODE=none (local
// dev), the token is unused — requests work without it.

const TOKEN_STORAGE_KEY = 'snowtuner.api_token'

export function getApiToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY)
  } catch {
    return null
  }
}

export function setApiToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_STORAGE_KEY, token)
    else localStorage.removeItem(TOKEN_STORAGE_KEY)
  } catch {
    /* localStorage unavailable — degrade silently */
  }
}

class ApiError extends Error {
  status: number
  body: unknown
  constructor(status: number, body: unknown, message: string) {
    super(message)
    this.status = status
    this.body = body
  }
}

async function request<T>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  opts?: { query?: Record<string, string | number | undefined>; body?: unknown },
): Promise<T> {
  const url = new URL(`${BASE}${path}`, window.location.origin)
  if (opts?.query) {
    for (const [k, v] of Object.entries(opts.query)) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v))
    }
  }
  const headers: Record<string, string> = {}
  if (opts?.body) headers['content-type'] = 'application/json'
  const token = getApiToken()
  if (token) headers['authorization'] = `Bearer ${token}`

  const res = await fetch(url, {
    method,
    headers,
    body: opts?.body ? JSON.stringify(opts.body) : undefined,
  })
  const text = await res.text()
  const parsed = text ? safeParse(text) : null
  if (!res.ok) {
    // FastAPI 4xx/5xx bodies are ``{detail: string | object}``; surface the
    // string form to error messages, fall back to HTTP statusText otherwise.
    const detail =
      parsed && typeof parsed === 'object' && 'detail' in parsed
        ? (parsed as { detail: unknown }).detail
        : null
    const msg = typeof detail === 'string' ? detail : res.statusText
    throw new ApiError(res.status, parsed, `${method} ${path}: ${msg}`)
  }
  return parsed as T
}

function safeParse(s: string): unknown {
  try {
    return JSON.parse(s)
  } catch {
    return s
  }
}

export const api = {
  status: () => request<Status>('GET', '/status'),
  warehouses: () => request<Warehouse[]>('GET', '/warehouses'),
  recommenders: () => request<RecommenderInfo[]>('GET', '/recommenders'),

  listRecommendations: (params?: { status?: RecommendationStatus; limit?: number }) =>
    request<Recommendation[]>('GET', '/recommendations', { query: params }),
  getRecommendation: (id: number) => request<Recommendation>('GET', `/recommendations/${id}`),
  acceptRecommendation: (id: number, note?: string) =>
    request<Recommendation>('POST', `/recommendations/${id}/accept`, { body: { note: note ?? null } }),
  rejectRecommendation: (id: number, note?: string) =>
    request<Recommendation>('POST', `/recommendations/${id}/reject`, { body: { note: note ?? null } }),

  listAutonomousConfig: () => request<AutonomousConfig[]>('GET', '/autonomous/config'),
  upsertAutonomousConfig: (
    actionType: string,
    warehouseName: string,
    knob: string,
    body: paths['/autonomous/config/{action_type}/{warehouse_name}/{knob}']['put']['requestBody']['content']['application/json'],
  ) =>
    request<AutonomousConfig>(
      'PUT',
      `/autonomous/config/${encodeURIComponent(actionType)}/${encodeURIComponent(warehouseName)}/${encodeURIComponent(knob)}`,
      { body },
    ),
  deleteAutonomousConfig: (actionType: string, warehouseName: string, knob: string) =>
    request<{ status: string }>(
      'DELETE',
      `/autonomous/config/${encodeURIComponent(actionType)}/${encodeURIComponent(warehouseName)}/${encodeURIComponent(knob)}`,
    ),
  resetAutonomousCircuit: (actionType: string, warehouseName: string, knob: string) =>
    request<{ status: string }>(
      'POST',
      `/autonomous/config/${encodeURIComponent(actionType)}/${encodeURIComponent(warehouseName)}/${encodeURIComponent(knob)}/reset-circuit`,
    ),
  listAutonomousApplications: (params?: { warehouse?: string; limit?: number }) =>
    request<AutonomousApplication[]>('GET', '/autonomous/applications', { query: params }),
  rollbackAutonomousApplication: (id: number) =>
    request<{ status: string; application_id: string }>(
      'POST',
      `/autonomous/applications/${id}/rollback`,
    ),

  credentials: () => request<CredentialStatus>('GET', '/credentials'),
  verifyCredentials: () => request<CredentialVerify>('POST', '/credentials/verify'),

  // ── Sync + automation ──────────────────────────────────────────
  runSync: () =>
    request<{ sync_results: Array<{ source_name: string; rows_ingested: number; duration_seconds: number; high_water: string | null }>; errors?: Array<{ source_name: string; error: string }> }>(
      'POST', '/sync/run',
    ),
  runBackfill: (days: number, source?: string) =>
    request<{ sync_results: Array<{ source_name: string; rows_ingested: number }> ; errors: Array<{ source_name: string; error: string }> }>(
      'POST', '/sync/backfill', { query: { days, source } },
    ),
  automationStatus: () =>
    request<Schemas['AutomationStatusOut']>('GET', '/automation/status'),
  runAutomationNow: () =>
    request<Schemas['TickReportOut']>('POST', '/automation/run-now'),

  // ── Queries explorer ───────────────────────────────────────────
  listQueries: (filters?: QueryListFilters) =>
    request<QueryListResponse>('GET', '/queries', {
      query: filters
        ? Object.fromEntries(
            Object.entries(filters).map(([k, v]) => [
              k,
              typeof v === 'boolean' ? String(v) : (v as string | number | undefined),
            ]),
          )
        : undefined,
    }),
  getQuery: (id: string) => request<QueryDetail>('GET', `/queries/${encodeURIComponent(id)}`),
  listQueryFamilies: (
    filters?: Omit<QueryListFilters, 'parameterized_hash' | 'has_queueing' | 'offset'>,
  ) =>
    request<QueryFamily[]>('GET', '/query-families', {
      query: filters
        ? Object.fromEntries(
            Object.entries(filters).map(([k, v]) => [
              k,
              typeof v === 'boolean' ? String(v) : (v as string | number | undefined),
            ]),
          )
        : undefined,
    }),
  queryFacets: (lookbackDays?: number) =>
    request<QueryFilterFacets>('GET', '/queries/facets', {
      query: { lookback_days: lookbackDays },
    }),

  // ── Query groups ───────────────────────────────────────────────
  listQueryGroups: (kind?: QueryGroupKind) =>
    request<QueryGroup[]>('GET', '/query-groups', { query: { kind } }),
  getQueryGroup: (id: number) => request<QueryGroup>('GET', `/query-groups/${id}`),
  createQueryGroup: (body: CreateQueryGroupBody) =>
    request<QueryGroup>('POST', '/query-groups', { body }),
  deleteQueryGroup: (id: number) =>
    request<{ status: string; id: string }>('DELETE', `/query-groups/${id}`),
  queryGroupMembers: (id: number, params?: { limit?: number; offset?: number }) =>
    request<QueryListResponse>('GET', `/query-groups/${id}/members`, { query: params }),

  // ── Self-documentation (Docs tab) ─────────────────────────────
  cliHelp: () => request<CliCommand>('GET', '/cli-help'),
  mcpTools: () => request<McpToolInfo[]>('GET', '/mcp-tools'),

  // ── Experiments (v0.2) ─────────────────────────────────────────
  listExperimentRecipes: () => request<RecipeInfo[]>('GET', '/experiments/recipes'),
  listExperiments: (params?: { status?: ExperimentStatus; target_warehouse?: string; limit?: number }) =>
    request<Experiment[]>('GET', '/experiments', { query: params }),
  getExperiment: (id: number) => request<Experiment>('GET', `/experiments/${id}`),
  listExperimentRuns: (id: number, armName?: string) =>
    request<ExperimentRun[]>('GET', `/experiments/${id}/runs`, { query: { arm_name: armName } }),
  proposeExperiment: (
    recipeName: string,
    targetWarehouse: string,
    queryGroupId?: number | null,
  ) =>
    request<Experiment>('POST', '/experiments/propose', {
      body: {
        recipe_name: recipeName,
        target_warehouse: targetWarehouse,
        query_group_id: queryGroupId ?? null,
      },
    }),
  proposeBenchmarkExperiment: (body: ProposeBenchmarkRequest) =>
    request<Experiment>('POST', '/experiments/propose-benchmark', { body }),
  acceptExperiment: (id: number) => request<Experiment>('POST', `/experiments/${id}/accept`),
  rejectExperiment: (id: number) => request<Experiment>('POST', `/experiments/${id}/reject`),
  runExperiment: (id: number) => request<Experiment>('POST', `/experiments/${id}/run`),
  abortExperiment: (id: number, reason: string) =>
    request<Experiment>('POST', `/experiments/${id}/abort`, { body: { reason } }),
  /**
   * Remove one query from a PROPOSED experiment's frozen workload.  The
   * server re-estimates cost from the remaining queries and returns the
   * updated Experiment.  Refuses with 409 if the experiment isn't
   * PROPOSED, and 422 if removing would empty the workload.
   */
  removeSampledQuery: (experimentId: number, queryId: string) =>
    request<Experiment>(
      'DELETE',
      `/experiments/${experimentId}/sampled-queries/${encodeURIComponent(queryId)}`,
    ),
}

export { ApiError }
