import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { ArrowLeft, Beaker, CheckCircle2, AlertTriangle, XCircle } from 'lucide-react'
import { api, type Experiment, type ExperimentStatus } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'

export const Route = createFileRoute('/experiments/$id')({
  parseParams: ({ id }) => ({ id: Number(id) }),
  component: ExperimentDetail,
})

function ExperimentDetail() {
  const { id } = Route.useParams()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const exp = useQuery({
    queryKey: ['experiment', id],
    queryFn: () => api.getExperiment(id),
    // Poll every 5s if RUNNING / ACCEPTED so the UI tracks lifecycle.
    refetchInterval: (q) => {
      const s = q.state.data?.status
      return s === 'RUNNING' || s === 'ACCEPTED' ? 5_000 : false
    },
  })

  const runs = useQuery({
    queryKey: ['experiment-runs', id],
    queryFn: () => api.listExperimentRuns(id),
    enabled: !!exp.data && exp.data.status !== 'PROPOSED' && exp.data.status !== 'REJECTED',
  })

  const accept = useMutation({
    mutationFn: () => api.acceptExperiment(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['experiment', id] }),
  })
  const reject = useMutation({
    mutationFn: () => api.rejectExperiment(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['experiment', id] }),
  })
  const run = useMutation({
    mutationFn: () => api.runExperiment(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['experiment', id] }),
  })
  const [abortReason, setAbortReason] = useState('')
  const abort = useMutation({
    mutationFn: () => api.abortExperiment(id, abortReason),
    onSuccess: () => {
      setAbortReason('')
      qc.invalidateQueries({ queryKey: ['experiment', id] })
    },
  })

  if (exp.isLoading) return <div className="p-6 text-sm">Loading…</div>
  if (exp.error) return <div className="p-6 text-sm text-destructive">{String(exp.error)}</div>
  if (!exp.data) return null
  const e = exp.data
  const linkedWh = e.proposed.target_warehouse ?? e.proposed.workload_warehouse
  const whRelation = e.proposed.kind === 'benchmark' ? 'workload from' : 'on'

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="mb-6 flex items-center gap-3">
        <Button variant="ghost" size="icon" onClick={() => navigate({ to: '/experiments' })}>
          <ArrowLeft className="h-4 w-4" />
        </Button>
        <Beaker className="h-6 w-6 text-primary/80" />
        <h1 className="text-2xl font-semibold">
          Experiment #{e.id} · {e.proposed.recipe_name}
        </h1>
        <Badge variant={e.proposed.kind === 'tuning' ? 'secondary' : 'outline'}>
          {e.proposed.kind}
        </Badge>
        <StatusBadge status={e.status} />
        {linkedWh && (
          <Link
            to="/warehouses/$name"
            params={{ name: linkedWh }}
            className="ml-2 text-sm text-primary hover:underline"
          >
            {whRelation} {linkedWh} →
          </Link>
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Hypothesis</CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm">{e.proposed.hypothesis}</p>
            <div className="mt-4 grid grid-cols-2 gap-4 text-sm">
              <Stat label="Sample size" value={`${e.proposed.sample_size} queries`} />
              <Stat label="Reps per arm" value={String(e.proposed.reps_per_arm)} />
              <Stat
                label="Cost estimate"
                value={`${e.proposed.cost_estimate.low_credits.toFixed(2)} – ${e.proposed.cost_estimate.high_credits.toFixed(2)} cr`}
              />
              <Stat
                label="Actual cost"
                value={
                  e.actual_cost_credits != null
                    ? `${e.actual_cost_credits.toFixed(4)} cr${e.cost_cap_hit ? ' (cap hit)' : ''}`
                    : '—'
                }
              />
            </div>
            <p className="mt-3 text-xs text-muted-foreground">
              {e.proposed.cost_estimate.rationale}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Actions</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {e.status === 'PROPOSED' && (
              <>
                <Button
                  onClick={() => accept.mutate()}
                  disabled={accept.isPending}
                  className="w-full"
                >
                  Accept
                </Button>
                <Button
                  variant="outline"
                  onClick={() => reject.mutate()}
                  disabled={reject.isPending}
                  className="w-full"
                >
                  Reject
                </Button>
              </>
            )}
            {e.status === 'ACCEPTED' && (
              <Button onClick={() => run.mutate()} disabled={run.isPending} className="w-full">
                {run.isPending ? 'Starting…' : 'Run experiment'}
              </Button>
            )}
            {(e.status === 'ACCEPTED' || e.status === 'RUNNING') && (
              <div className="space-y-1.5">
                <input
                  type="text"
                  className="w-full rounded-md border bg-background px-2 py-1.5 text-sm"
                  placeholder="Reason to abort…"
                  value={abortReason}
                  onChange={(ev) => setAbortReason(ev.target.value)}
                />
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => abort.mutate()}
                  disabled={!abortReason || abort.isPending}
                  className="w-full"
                >
                  Abort
                </Button>
              </div>
            )}
            {(accept.error || reject.error || run.error || abort.error) && (
              <div className="text-xs text-destructive">
                {String(accept.error || reject.error || run.error || abort.error)}
              </div>
            )}
            {e.aborted_reason && (
              <div className="rounded-md border border-destructive/30 bg-destructive/5 p-2 text-xs">
                <strong>Aborted:</strong> {e.aborted_reason}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card className="mt-4">
        <CardHeader>
          <CardTitle>Arms ({e.proposed.arms.length})</CardTitle>
        </CardHeader>
        <CardContent>
          <table className="w-full text-sm">
            <thead className="border-b text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-2 py-2">Name</th>
                <th className="px-2 py-2">Delta from control</th>
                <th className="px-2 py-2">Eligibility</th>
              </tr>
            </thead>
            <tbody>
              {e.proposed.arms.map((arm) => (
                <tr key={arm.name} className="border-b">
                  <td className="px-2 py-2 font-mono">{arm.name}</td>
                  <td className="px-2 py-2 font-mono text-xs">
                    {Object.entries(arm.delta)
                      .filter(([, v]) => v !== null && v !== undefined)
                      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
                      .join(', ') || <span className="text-muted-foreground">control</span>}
                  </td>
                  <td className="px-2 py-2 text-xs">
                    {arm.eligibility_issues.length === 0 ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      arm.eligibility_issues.map((i, idx) => (
                        <div
                          key={idx}
                          className={i.severity === 'error' ? 'text-destructive' : 'text-yellow-600'}
                        >
                          {i.severity}: {i.message}
                        </div>
                      ))
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </CardContent>
      </Card>

      {e.report && <ReportCard experiment={e} />}

      {runs.data && runs.data.length > 0 && (
        <Card className="mt-4">
          <CardHeader>
            <CardTitle>Runs ({runs.data.length})</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <RunsTable runs={runs.data} />
          </CardContent>
        </Card>
      )}
    </div>
  )
}

function ReportCard({ experiment }: { experiment: Experiment }) {
  const r = experiment.report!
  const isBenchmark = experiment.proposed.kind === 'benchmark'
  return (
    <Card className="mt-4">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          Report
          {r.best_arm_name ? (
            <CheckCircle2 className="h-5 w-5 text-green-600" />
          ) : (
            <AlertTriangle className="h-5 w-5 text-yellow-600" />
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        <div>
          <div className="text-sm font-medium">
            {isBenchmark ? 'Pareto-optimal pick' : 'Best arm'}
          </div>
          {r.best_arm_name ? (
            <div className="mt-1">
              <Badge variant="default" className="mr-2">
                {r.best_arm_name}
              </Badge>
              <span className="text-sm">{r.best_arm_rationale}</span>
            </div>
          ) : (
            <div className="mt-1 text-sm text-muted-foreground">
              {isBenchmark
                ? 'No arm produced successful runs.'
                : 'No arm satisfies the win criteria (credits savings with confidence AND no unacceptable p95 latency regression).'}
            </div>
          )}
        </div>

        {r.projected_annual_savings_low_credits != null &&
          r.projected_annual_savings_high_credits != null && (
            <div>
              <div className="text-sm font-medium">Projected annual savings</div>
              <div className="text-sm">
                {r.projected_annual_savings_low_credits.toFixed(0)}–
                {r.projected_annual_savings_high_credits.toFixed(0)} credits
              </div>
              {r.projected_p95_latency_delta_pct_low != null &&
                r.projected_p95_latency_delta_pct_high != null && (
                  <div className="text-xs text-muted-foreground">
                    p95 latency change: {r.projected_p95_latency_delta_pct_low.toFixed(1)}% to{' '}
                    {r.projected_p95_latency_delta_pct_high.toFixed(1)}%
                  </div>
                )}
            </div>
          )}

        {isBenchmark ? (
          /* Benchmark: absolute stats per arm + Pareto markers */
          <table className="w-full text-sm">
            <thead className="border-b text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-2 py-2">Arm</th>
                <th className="px-2 py-2 text-right">n</th>
                <th className="px-2 py-2 text-right">Mean elapsed (ms)</th>
                <th className="px-2 py-2 text-right">p95 elapsed (ms)</th>
                <th className="px-2 py-2 text-right">Credits / query</th>
                <th className="px-2 py-2">Frontier</th>
              </tr>
            </thead>
            <tbody>
              {r.arms.map((a) => (
                <tr key={a.arm_name} className="border-b">
                  <td className="px-2 py-2 font-mono">{a.arm_name}</td>
                  <td className="px-2 py-2 text-right">{a.n_queries_run}</td>
                  <td className="px-2 py-2 text-right font-mono">
                    {a.elapsed_ms_mean.toFixed(0)}
                  </td>
                  <td className="px-2 py-2 text-right font-mono">
                    {a.elapsed_ms_p95.toFixed(0)}
                  </td>
                  <td className="px-2 py-2 text-right font-mono">
                    {a.credits_per_query_mean.toFixed(5)}
                  </td>
                  <td className="px-2 py-2">
                    {a.is_pareto_optimal ? (
                      <Badge variant="default" className="text-xs">
                        ★ Pareto
                      </Badge>
                    ) : (
                      <span className="text-muted-foreground text-xs">dominated</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          /* Tuning: paired deltas vs control */
          <table className="w-full text-sm">
            <thead className="border-b text-left text-xs uppercase text-muted-foreground">
              <tr>
                <th className="px-2 py-2">Arm</th>
                <th className="px-2 py-2 text-right">n</th>
                <th className="px-2 py-2 text-right">Δ elapsed (ms)</th>
                <th className="px-2 py-2 text-right">95% CI</th>
                <th className="px-2 py-2 text-right">Δ credits/q</th>
                <th className="px-2 py-2 text-right">p (corrected)</th>
              </tr>
            </thead>
            <tbody>
              {r.arms.map((a) => (
                <tr key={a.arm_name} className="border-b">
                  <td className="px-2 py-2 font-mono">{a.arm_name}</td>
                  <td className="px-2 py-2 text-right">{a.n_queries_run}</td>
                  <td className="px-2 py-2 text-right font-mono">
                    {a.elapsed_ms_delta_mean.toFixed(0)}
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-xs">
                    [{a.elapsed_ms_delta_ci_low.toFixed(0)},{' '}
                    {a.elapsed_ms_delta_ci_high.toFixed(0)}]
                  </td>
                  <td className="px-2 py-2 text-right font-mono">
                    {a.credits_per_query_delta_mean.toFixed(5)}
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-xs">
                    {a.credits_p_value_corrected != null
                      ? formatP(a.credits_p_value_corrected)
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {r.sample_size_warnings.length > 0 && (
          <div className="rounded-md border border-yellow-500/30 bg-yellow-500/5 p-3">
            <div className="text-sm font-medium text-yellow-700">Sample-size warnings</div>
            <ul className="mt-1 space-y-0.5 text-xs">
              {r.sample_size_warnings.map((w, i) => (
                <li key={i}>• {w}</li>
              ))}
            </ul>
          </div>
        )}

        <details className="text-xs">
          <summary className="cursor-pointer font-medium">Statistical methodology</summary>
          <div className="mt-2 space-y-2 text-muted-foreground">
            <div>
              <strong>Corrections:</strong>
              <ul className="ml-4 list-disc">
                {r.statistical_corrections_applied.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </div>
            <div>
              <strong>Assumptions:</strong>
              <ul className="ml-4 list-disc">
                {r.assumptions.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
            </div>
            <div>Excluded query count: {r.excluded_query_count}</div>
          </div>
        </details>
      </CardContent>
    </Card>
  )
}

function RunsTable({ runs }: { runs: import('@/lib/api').ExperimentRun[] }) {
  // Compact: aggregated by (arm, sampled_query_id) with median elapsed.
  const grouped: Record<string, { arm: string; n: number; elapsedMs: number[]; failed: number }> =
    {}
  for (const r of runs) {
    const k = r.arm_name
    if (!grouped[k]) grouped[k] = { arm: k, n: 0, elapsedMs: [], failed: 0 }
    grouped[k].n += 1
    if (r.status === 'failed') grouped[k].failed += 1
    if (r.elapsed_ms != null) grouped[k].elapsedMs.push(r.elapsed_ms)
  }
  return (
    <table className="w-full text-sm">
      <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
        <tr>
          <th className="px-4 py-2">Arm</th>
          <th className="px-4 py-2 text-right">Runs</th>
          <th className="px-4 py-2 text-right">Failed</th>
          <th className="px-4 py-2 text-right">Median elapsed (ms)</th>
          <th className="px-4 py-2 text-right">p95 elapsed (ms)</th>
        </tr>
      </thead>
      <tbody>
        {Object.values(grouped).map((g) => {
          const sorted = [...g.elapsedMs].sort((a, b) => a - b)
          const median = sorted[Math.floor(sorted.length / 2)] ?? 0
          const p95 = sorted[Math.floor(sorted.length * 0.95)] ?? 0
          return (
            <tr key={g.arm} className="border-b">
              <td className="px-4 py-2 font-mono">{g.arm}</td>
              <td className="px-4 py-2 text-right">{g.n}</td>
              <td className="px-4 py-2 text-right">
                {g.failed > 0 ? (
                  <span className="inline-flex items-center gap-1 text-destructive">
                    <XCircle className="h-3 w-3" />
                    {g.failed}
                  </span>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
              <td className="px-4 py-2 text-right font-mono">{median}</td>
              <td className="px-4 py-2 text-right font-mono">{p95}</td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs uppercase text-muted-foreground">{label}</div>
      <div className="font-mono">{value}</div>
    </div>
  )
}

function StatusBadge({ status }: { status: ExperimentStatus }) {
  const variant: Record<ExperimentStatus, 'default' | 'secondary' | 'destructive' | 'outline'> = {
    PROPOSED: 'outline',
    ACCEPTED: 'secondary',
    RUNNING: 'default',
    COMPLETED: 'default',
    ABORTED: 'destructive',
    FAILED: 'destructive',
    REJECTED: 'outline',
  }
  return <Badge variant={variant[status]}>{status}</Badge>
}

function formatP(p: number): string {
  if (p < 0.001) return p.toExponential(1)
  return p.toFixed(3)
}
