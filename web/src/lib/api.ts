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

export type RecommendationStatus =
  | 'PROPOSED'
  | 'ACCEPTED'
  | 'REJECTED'
  | 'APPLIED'
  | 'ROLLED_BACK'
  | 'SUPERSEDED'

// ── Experiments (v0.2) ──────────────────────────────────────────
// Defined inline (not pulled from generated api-types) so the UI can ship
// without regenerating the OpenAPI client every time the experiments domain
// model changes.  The shapes mirror the backend Pydantic models.

export type ExperimentStatus =
  | 'PROPOSED'
  | 'ACCEPTED'
  | 'RUNNING'
  | 'COMPLETED'
  | 'ABORTED'
  | 'FAILED'
  | 'REJECTED'

export type ExperimentKind = 'tuning' | 'benchmark'

export interface RecipeInfo {
  name: string
  summary: string
}

export interface BenchmarkArmSpec {
  name: string
  size?: string | null              // e.g. 'XSMALL' through 'X6LARGE'
  generation?: string | null        // '1' or '2'
  qas_state?: string | null         // 'on' or 'off' (case-insensitive on the wire)
  qas_max_scale_factor?: number | null
}

export interface ProposeBenchmarkRequest {
  hypothesis: string
  workload_warehouse: string
  arms: BenchmarkArmSpec[]
  control_arm_name?: string | null
  sample_size?: number
  reps_per_arm?: number
}

export interface CostEstimate {
  low_credits: number
  high_credits: number
  rationale: string
  projected_annual_savings_low_credits?: number | null
  projected_annual_savings_high_credits?: number | null
}

export interface Issue {
  severity: 'warning' | 'error'
  message: string
  code?: string | null
}

export interface ArmConfigDelta {
  generation?: string | null
  size?: string | null
  qas_state?: string | null
  qas_max_scale_factor?: number | null
}

export interface Arm {
  name: string
  delta: ArmConfigDelta
  eligibility_issues: Issue[]
}

export interface ProposedExperiment {
  kind: ExperimentKind
  recipe_name: string
  target_warehouse: string | null
  workload_warehouse: string | null
  control_arm_name: string | null
  hypothesis: string
  arms: Arm[]
  sample_size: number
  reps_per_arm: number
  cost_estimate: CostEstimate
  eligibility_issues: Issue[]
  proposed_by: string
}

export interface ArmObservation {
  arm_name: string
  n_queries_run: number
  n_queries_failed: number
  n_queries_excluded: number
  // Absolute stats (always populated)
  elapsed_ms_mean: number
  elapsed_ms_p50: number
  elapsed_ms_p95: number
  credits_per_query_mean: number
  // Paired-delta stats (zero when no control)
  elapsed_ms_delta_mean: number
  elapsed_ms_delta_p50: number
  elapsed_ms_delta_p95: number
  elapsed_ms_delta_ci_low: number
  elapsed_ms_delta_ci_high: number
  credits_per_query_delta_mean: number
  credits_per_query_delta_ci_low: number
  credits_per_query_delta_ci_high: number
  elapsed_p_value_corrected?: number | null
  credits_p_value_corrected?: number | null
  // Pareto-frontier flag (benchmark only)
  is_pareto_optimal: boolean
}

export interface ExperimentReport {
  experiment_id: number
  arms: ArmObservation[]
  best_arm_name?: string | null
  best_arm_rationale?: string | null
  best_arm_objective?: string | null
  projected_annual_savings_low_credits?: number | null
  projected_annual_savings_high_credits?: number | null
  projected_p95_latency_delta_pct_low?: number | null
  projected_p95_latency_delta_pct_high?: number | null
  sample_size_warnings: string[]
  excluded_query_count: number
  statistical_corrections_applied: string[]
  assumptions: string[]
}

export interface Experiment {
  id: number
  proposed: ProposedExperiment
  status: ExperimentStatus
  proposed_at: string
  accepted_at?: string | null
  started_at?: string | null
  completed_at?: string | null
  aborted_reason?: string | null
  actual_cost_credits?: number | null
  cost_cap_hit: boolean
  report?: ExperimentReport | null
  derived_recommendation_id?: number | null
  test_warehouse_names: string[]
  test_warehouses_cleaned: boolean
}

// ── Queries explorer ──────────────────────────────────────────

export interface QueryRow {
  query_id: string
  query_text_preview: string
  query_type: string | null
  execution_status: string | null
  user_name: string | null
  role_name: string | null
  warehouse_name: string | null
  warehouse_size: string | null
  start_time: string | null
  total_elapsed_ms: number | null
  bytes_scanned: number | null
  bytes_spilled_to_local: number | null
  bytes_spilled_to_remote: number | null
  queued_overload_ms: number | null
  query_parameterized_hash: string | null
}

export interface QueryDetail {
  query_id: string
  query_text: string
  query_type: string | null
  execution_status: string | null
  user_name: string | null
  role_name: string | null
  warehouse_name: string | null
  warehouse_size: string | null
  database_name: string | null
  schema_name: string | null
  start_time: string | null
  end_time: string | null
  total_elapsed_ms: number | null
  compilation_ms: number | null
  execution_ms: number | null
  queued_overload_ms: number | null
  queued_provisioning_ms: number | null
  bytes_scanned: number | null
  bytes_spilled_to_local: number | null
  bytes_spilled_to_remote: number | null
  query_parameterized_hash: string | null
}

export interface QueryFamily {
  query_parameterized_hash: string
  representative_query_id: string
  representative_sql: string
  occurrence_count: number
  mean_elapsed_ms: number | null
  p95_elapsed_ms: number | null
  total_elapsed_ms: number | null
  total_bytes_scanned: number | null
  n_spill_remote: number
  n_failed: number
  first_seen: string | null
  last_seen: string | null
  distinct_warehouses: number
  distinct_users: number
}

export interface QueryListResponse {
  rows: QueryRow[]
  total: number
  limit: number
  offset: number
}

export interface QueryFilterFacets {
  warehouses: string[]
  users: string[]
  query_types: string[]
  execution_statuses: string[]
}

export interface QueryListFilters {
  warehouse?: string         // comma-separated
  user?: string
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
  limit?: number
  offset?: number
}

export interface ExperimentRun {
  experiment_id: number
  arm_name: string
  rep_index: number
  sampled_query_id: string
  parameterized_hash?: string | null
  replay_query_id?: string | null
  elapsed_ms?: number | null
  queued_overload_ms?: number | null
  bytes_scanned?: number | null
  bytes_spilled_local?: number | null
  bytes_spilled_remote?: number | null
  credits_used_estimate?: number | null
  status: 'success' | 'failed' | 'excluded'
  error_message?: string | null
  started_at?: string | null
  completed_at?: string | null
}

const BASE = '/api'

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
  const res = await fetch(url, {
    method,
    headers: opts?.body ? { 'content-type': 'application/json' } : undefined,
    body: opts?.body ? JSON.stringify(opts.body) : undefined,
  })
  const text = await res.text()
  const parsed = text ? safeParse(text) : null
  if (!res.ok) {
    const msg = (parsed && (parsed as any).detail) || res.statusText
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

  // ── Experiments (v0.2) ─────────────────────────────────────────
  listExperimentRecipes: () => request<RecipeInfo[]>('GET', '/experiments/recipes'),
  listExperiments: (params?: { status?: ExperimentStatus; target_warehouse?: string; limit?: number }) =>
    request<Experiment[]>('GET', '/experiments', { query: params }),
  getExperiment: (id: number) => request<Experiment>('GET', `/experiments/${id}`),
  listExperimentRuns: (id: number, armName?: string) =>
    request<ExperimentRun[]>('GET', `/experiments/${id}/runs`, { query: { arm_name: armName } }),
  proposeExperiment: (recipeName: string, targetWarehouse: string) =>
    request<Experiment>('POST', '/experiments/propose', {
      body: { recipe_name: recipeName, target_warehouse: targetWarehouse },
    }),
  proposeBenchmarkExperiment: (body: ProposeBenchmarkRequest) =>
    request<Experiment>('POST', '/experiments/propose-benchmark', { body }),
  acceptExperiment: (id: number) => request<Experiment>('POST', `/experiments/${id}/accept`),
  rejectExperiment: (id: number) => request<Experiment>('POST', `/experiments/${id}/reject`),
  runExperiment: (id: number) => request<Experiment>('POST', `/experiments/${id}/run`),
  abortExperiment: (id: number, reason: string) =>
    request<Experiment>('POST', `/experiments/${id}/abort`, { body: { reason } }),
}

export { ApiError }
