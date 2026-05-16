import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Beaker, Plus } from 'lucide-react'
import { api, type ExperimentKind, type ExperimentStatus } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'

const ALL_STATUSES: ExperimentStatus[] = [
  'PROPOSED',
  'ACCEPTED',
  'RUNNING',
  'COMPLETED',
  'ABORTED',
  'FAILED',
  'REJECTED',
]

type ExperimentsSearch = {
  status?: ExperimentStatus
  target_warehouse?: string
}

export const Route = createFileRoute('/experiments/')({
  validateSearch: (search: Record<string, unknown>): ExperimentsSearch => {
    const s = String(search.status ?? '').toUpperCase()
    const status = (ALL_STATUSES as string[]).includes(s)
      ? (s as ExperimentStatus)
      : undefined
    const tw = typeof search.target_warehouse === 'string' ? search.target_warehouse : undefined
    return { status, target_warehouse: tw }
  },
  component: ExperimentsList,
})

function ExperimentsList() {
  const search = Route.useSearch()
  const navigate = useNavigate({ from: '/experiments/' })
  const qc = useQueryClient()

  const list = useQuery({
    queryKey: ['experiments', search.status, search.target_warehouse],
    queryFn: () =>
      api.listExperiments({
        status: search.status,
        target_warehouse: search.target_warehouse,
        limit: 200,
      }),
  })

  const [showNew, setShowNew] = useState(false)

  function setStatus(s: ExperimentStatus | undefined) {
    navigate({ search: { ...search, status: s } })
  }

  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="mb-6 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Beaker className="h-6 w-6 text-primary/80" />
          <h1 className="text-2xl font-semibold">Experiments</h1>
          <span className="text-sm text-muted-foreground">
            A/B-style replay experiments against side-by-side warehouse configs
          </span>
        </div>
        <Button onClick={() => setShowNew(true)} className="gap-1.5">
          <Plus className="h-4 w-4" />
          New experiment
        </Button>
      </div>

      {showNew && (
        <NewExperimentDialog
          onClose={() => setShowNew(false)}
          onCreated={() => {
            setShowNew(false)
            qc.invalidateQueries({ queryKey: ['experiments'] })
          }}
        />
      )}

      <div className="mb-4 flex flex-wrap items-center gap-2 text-sm">
        <span className="text-muted-foreground">Status:</span>
        <button
          onClick={() => setStatus(undefined)}
          className={`rounded-full px-3 py-1 ${search.status === undefined ? 'bg-primary text-primary-foreground' : 'bg-secondary hover:bg-accent'}`}
        >
          All
        </button>
        {ALL_STATUSES.map((s) => (
          <button
            key={s}
            onClick={() => setStatus(s)}
            className={`rounded-full px-3 py-1 ${search.status === s ? 'bg-primary text-primary-foreground' : 'bg-secondary hover:bg-accent'}`}
          >
            {s}
          </button>
        ))}
        {search.target_warehouse && (
          <span className="ml-2 text-xs text-muted-foreground">
            warehouse=
            <code className="font-mono">{search.target_warehouse}</code>
            <button
              onClick={() => navigate({ search: { ...search, target_warehouse: undefined } })}
              className="ml-1 underline"
            >
              clear
            </button>
          </span>
        )}
      </div>

      <Card>
        <CardContent className="p-0">
          {list.isLoading && <div className="p-4 text-sm text-muted-foreground">Loading…</div>}
          {list.data && list.data.length === 0 && (
            <div className="p-8 text-center text-sm text-muted-foreground">
              No experiments. Click <span className="font-medium">New experiment</span> to
              create one.
            </div>
          )}
          {list.data && list.data.length > 0 && (
            <table className="w-full text-sm">
              <thead className="border-b bg-muted/40 text-left text-xs uppercase text-muted-foreground">
                <tr>
                  <th className="px-4 py-2 text-right">#</th>
                  <th className="px-4 py-2">Kind</th>
                  <th className="px-4 py-2">Recipe</th>
                  <th className="px-4 py-2">Warehouse</th>
                  <th className="px-4 py-2">Status</th>
                  <th className="px-4 py-2 text-right">Arms</th>
                  <th className="px-4 py-2">Cost estimate</th>
                  <th className="px-4 py-2">Best arm</th>
                  <th className="px-4 py-2">Proposed</th>
                </tr>
              </thead>
              <tbody>
                {list.data.map((e) => {
                  const linkedWh =
                    e.proposed.target_warehouse ?? e.proposed.workload_warehouse
                  return (
                    <tr key={e.id} className="border-b hover:bg-muted/30">
                      <td className="px-4 py-2 text-right font-mono text-xs">{e.id}</td>
                      <td className="px-4 py-2">
                        <KindBadge kind={e.proposed.kind} />
                      </td>
                      <td className="px-4 py-2 font-medium">{e.proposed.recipe_name}</td>
                      <td className="px-4 py-2">
                        {linkedWh ? (
                          <Link
                            to="/warehouses/$name"
                            params={{ name: linkedWh }}
                            className="text-primary hover:underline"
                          >
                            {linkedWh}
                            {e.proposed.kind === 'benchmark' && (
                              <span className="ml-1 text-xs text-muted-foreground">(workload)</span>
                            )}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-4 py-2">
                        <StatusBadge status={e.status} />
                      </td>
                      <td className="px-4 py-2 text-right">{e.proposed.arms.length}</td>
                      <td className="px-4 py-2 font-mono text-xs">
                        {e.proposed.cost_estimate.low_credits.toFixed(2)}–
                        {e.proposed.cost_estimate.high_credits.toFixed(2)} cr
                      </td>
                      <td className="px-4 py-2 text-xs">
                        {e.report?.best_arm_name ?? <span className="text-muted-foreground">—</span>}
                      </td>
                      <td className="px-4 py-2">
                        <Link
                          to="/experiments/$id"
                          params={{ id: e.id }}
                          className="text-primary hover:underline"
                        >
                          View →
                        </Link>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function KindBadge({ kind }: { kind: ExperimentKind }) {
  const label = kind === 'tuning' ? 'tune' : 'benchmark'
  return (
    <Badge variant={kind === 'tuning' ? 'secondary' : 'outline'}>
      {label}
    </Badge>
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

function NewExperimentDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: () => void
}) {
  const [tab, setTab] = useState<'tune' | 'benchmark'>('tune')

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <Card className="w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        <CardHeader>
          <CardTitle>Propose a new experiment</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {/* Tab strip */}
          <div className="grid grid-cols-2 gap-2">
            <button
              onClick={() => setTab('tune')}
              className={`rounded-md border p-3 text-left transition-colors ${
                tab === 'tune'
                  ? 'border-primary bg-primary/5'
                  : 'border-input hover:bg-accent'
              }`}
            >
              <div className="text-sm font-medium">Tune a warehouse</div>
              <div className="mt-1 text-xs text-muted-foreground">
                Find a price-performance improvement for an existing warehouse and its workload.
              </div>
            </button>
            <button
              onClick={() => setTab('benchmark')}
              className={`rounded-md border p-3 text-left transition-colors ${
                tab === 'benchmark'
                  ? 'border-primary bg-primary/5'
                  : 'border-input hover:bg-accent'
              }`}
            >
              <div className="text-sm font-medium">Compare configurations</div>
              <div className="mt-1 text-xs text-muted-foreground">
                Benchmark a set of queries across several candidate warehouse configs. Produces a comparison report.
              </div>
            </button>
          </div>

          {tab === 'tune' ? (
            <TuneForm onClose={onClose} onCreated={onCreated} />
          ) : (
            <BenchmarkForm onClose={onClose} onCreated={onCreated} />
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function TuneForm({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: () => void
}) {
  const recipes = useQuery({
    queryKey: ['experiment-recipes'],
    queryFn: api.listExperimentRecipes,
  })
  const warehouses = useQuery({
    queryKey: ['warehouses'],
    queryFn: api.warehouses,
  })
  const [recipeName, setRecipeName] = useState<string>('')
  const [target, setTarget] = useState<string>('')
  const [error, setError] = useState<string | null>(null)

  const propose = useMutation({
    mutationFn: () => api.proposeExperiment(recipeName, target),
    onSuccess: onCreated,
    onError: (e: Error) => setError(e.message),
  })

  return (
    <div className="space-y-4">
      <div>
        <label className="text-sm font-medium">Recipe</label>
        <select
          className="mt-1 w-full rounded-md border bg-background px-3 py-1.5 text-sm"
          value={recipeName}
          onChange={(e) => setRecipeName(e.target.value)}
        >
          <option value="">— select a recipe —</option>
          {recipes.data?.map((r) => (
            <option key={r.name} value={r.name}>
              {r.name} — {r.summary}
            </option>
          ))}
        </select>
      </div>
      <div>
        <label className="text-sm font-medium">Warehouse to optimize</label>
        <select
          className="mt-1 w-full rounded-md border bg-background px-3 py-1.5 text-sm"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
        >
          <option value="">— select a warehouse —</option>
          {warehouses.data?.map((w) => (
            <option key={w.name} value={w.name}>
              {w.name} ({w.size ?? '?'})
            </option>
          ))}
        </select>
        <p className="mt-1 text-xs text-muted-foreground">
          snowtuner samples real queries from this warehouse and replays them against side-by-side
          clones — your production warehouse is never touched.
        </p>
      </div>
      {error && <div className="text-sm text-destructive">{error}</div>}
      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          onClick={() => propose.mutate()}
          disabled={!recipeName || !target || propose.isPending}
        >
          {propose.isPending ? 'Proposing…' : 'Propose'}
        </Button>
      </div>
    </div>
  )
}

// ── Benchmark form ──────────────────────────────────────────────────

interface ArmDraft {
  name: string
  size: string
  generation: '1' | '2' | ''
  qas_state: 'on' | 'off' | ''
  qas_max_scale_factor: string  // string so the input is controlled cleanly
}

const SIZE_OPTIONS = [
  '', 'XSMALL', 'SMALL', 'MEDIUM', 'LARGE', 'XLARGE',
  'X2LARGE', 'X3LARGE', 'X4LARGE', 'X5LARGE', 'X6LARGE',
]

function emptyArm(idx: number): ArmDraft {
  return {
    name: `arm_${idx + 1}`,
    size: 'MEDIUM',
    generation: '1',
    qas_state: 'off',
    qas_max_scale_factor: '',
  }
}

function BenchmarkForm({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: () => void
}) {
  const warehouses = useQuery({
    queryKey: ['warehouses'],
    queryFn: api.warehouses,
  })

  const [hypothesis, setHypothesis] = useState('')
  const [workload, setWorkload] = useState('')
  const [arms, setArms] = useState<ArmDraft[]>([emptyArm(0), emptyArm(1)])
  const [controlArmName, setControlArmName] = useState<string>('')   // '' = no control
  const [sampleSize, setSampleSize] = useState<number>(30)
  const [repsPerArm, setRepsPerArm] = useState<number>(3)
  const [error, setError] = useState<string | null>(null)

  function updateArm(idx: number, patch: Partial<ArmDraft>) {
    setArms((prev) => prev.map((a, i) => (i === idx ? { ...a, ...patch } : a)))
  }
  function addArm() {
    setArms((prev) => [...prev, emptyArm(prev.length)])
  }
  function removeArm(idx: number) {
    setArms((prev) => prev.filter((_, i) => i !== idx))
    // If the removed arm was the designated control, clear control selection.
    const removed = arms[idx]
    if (removed && removed.name === controlArmName) setControlArmName('')
  }

  const propose = useMutation({
    mutationFn: () => {
      const body = {
        hypothesis,
        workload_warehouse: workload,
        control_arm_name: controlArmName || null,
        sample_size: sampleSize,
        reps_per_arm: repsPerArm,
        arms: arms.map((a) => ({
          name: a.name,
          size: a.size || null,
          generation: a.generation || null,
          qas_state: a.qas_state || null,
          qas_max_scale_factor: a.qas_max_scale_factor
            ? Number(a.qas_max_scale_factor)
            : null,
        })),
      }
      return api.proposeBenchmarkExperiment(body)
    },
    onSuccess: onCreated,
    onError: (e: Error) => setError(e.message),
  })

  const valid = hypothesis.trim().length > 0 && workload && arms.length >= 2 &&
    arms.every((a) => a.name.trim().length > 0)
  const armNames = arms.map((a) => a.name)
  const hasDuplicateName = new Set(armNames).size !== armNames.length

  return (
    <div className="space-y-4">
      <div>
        <label className="text-sm font-medium">Hypothesis</label>
        <input
          type="text"
          className="mt-1 w-full rounded-md border bg-background px-3 py-1.5 text-sm"
          placeholder="e.g. Does MEDIUM perform as well as LARGE on the ETL workload?"
          value={hypothesis}
          onChange={(e) => setHypothesis(e.target.value)}
        />
      </div>

      <div>
        <label className="text-sm font-medium">Workload source (warehouse to sample queries from)</label>
        <select
          className="mt-1 w-full rounded-md border bg-background px-3 py-1.5 text-sm"
          value={workload}
          onChange={(e) => setWorkload(e.target.value)}
        >
          <option value="">— select a warehouse —</option>
          {warehouses.data?.map((w) => (
            <option key={w.name} value={w.name}>
              {w.name} ({w.size ?? '?'})
            </option>
          ))}
        </select>
        <p className="mt-1 text-xs text-muted-foreground">
          Sampled queries are replayed against the test warehouses you configure below. The
          workload warehouse itself isn't touched.
        </p>
      </div>

      <div>
        <div className="mb-2 flex items-center justify-between">
          <label className="text-sm font-medium">Arms ({arms.length})</label>
          <Button variant="outline" size="sm" onClick={addArm}>
            + Add arm
          </Button>
        </div>
        <div className="space-y-2">
          {arms.map((arm, idx) => (
            <ArmEditor
              key={idx}
              arm={arm}
              onChange={(patch) => updateArm(idx, patch)}
              onRemove={arms.length > 2 ? () => removeArm(idx) : undefined}
              isControl={controlArmName === arm.name && arm.name !== ''}
              onSetControl={() =>
                setControlArmName(controlArmName === arm.name ? '' : arm.name)
              }
            />
          ))}
        </div>
        {hasDuplicateName && (
          <div className="mt-2 text-xs text-destructive">Arm names must be unique.</div>
        )}
        <p className="mt-2 text-xs text-muted-foreground">
          Mark one arm as the reference control to get paired stats vs. that arm. Leave all unmarked
          for a pure Pareto comparison.
        </p>
      </div>

      <details className="text-sm">
        <summary className="cursor-pointer font-medium">Advanced settings</summary>
        <div className="mt-2 grid grid-cols-2 gap-3">
          <div>
            <label className="text-xs text-muted-foreground">Sample size</label>
            <input
              type="number"
              min={1}
              max={500}
              className="mt-1 w-full rounded-md border bg-background px-2 py-1 text-sm"
              value={sampleSize}
              onChange={(e) => setSampleSize(Number(e.target.value))}
            />
          </div>
          <div>
            <label className="text-xs text-muted-foreground">Reps per arm</label>
            <input
              type="number"
              min={1}
              max={10}
              className="mt-1 w-full rounded-md border bg-background px-2 py-1 text-sm"
              value={repsPerArm}
              onChange={(e) => setRepsPerArm(Number(e.target.value))}
            />
          </div>
        </div>
      </details>

      {error && <div className="text-sm text-destructive">{error}</div>}

      <div className="flex justify-end gap-2 pt-2">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button
          onClick={() => propose.mutate()}
          disabled={!valid || hasDuplicateName || propose.isPending}
        >
          {propose.isPending ? 'Proposing…' : 'Propose'}
        </Button>
      </div>
    </div>
  )
}

function ArmEditor({
  arm,
  onChange,
  onRemove,
  isControl,
  onSetControl,
}: {
  arm: ArmDraft
  onChange: (patch: Partial<ArmDraft>) => void
  onRemove?: () => void
  isControl: boolean
  onSetControl: () => void
}) {
  return (
    <div className={`rounded-md border p-3 ${isControl ? 'border-primary bg-primary/5' : 'border-input'}`}>
      <div className="mb-2 flex items-center gap-2">
        <input
          type="text"
          className="flex-1 rounded-md border bg-background px-2 py-1 font-mono text-sm"
          placeholder="arm name"
          value={arm.name}
          onChange={(e) => onChange({ name: e.target.value })}
        />
        <label className="flex items-center gap-1 text-xs">
          <input
            type="checkbox"
            checked={isControl}
            onChange={onSetControl}
          />
          control
        </label>
        {onRemove && (
          <button
            onClick={onRemove}
            className="text-xs text-muted-foreground hover:text-destructive"
            aria-label="Remove arm"
          >
            ×
          </button>
        )}
      </div>
      <div className="grid grid-cols-4 gap-2 text-xs">
        <div>
          <label className="text-muted-foreground">size</label>
          <select
            className="mt-0.5 w-full rounded border bg-background px-1.5 py-1 text-xs"
            value={arm.size}
            onChange={(e) => onChange({ size: e.target.value })}
          >
            {SIZE_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s || '—'}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="text-muted-foreground">gen</label>
          <select
            className="mt-0.5 w-full rounded border bg-background px-1.5 py-1 text-xs"
            value={arm.generation}
            onChange={(e) =>
              onChange({ generation: e.target.value as ArmDraft['generation'] })
            }
          >
            <option value="">—</option>
            <option value="1">1</option>
            <option value="2">2</option>
          </select>
        </div>
        <div>
          <label className="text-muted-foreground">QAS</label>
          <select
            className="mt-0.5 w-full rounded border bg-background px-1.5 py-1 text-xs"
            value={arm.qas_state}
            onChange={(e) =>
              onChange({ qas_state: e.target.value as ArmDraft['qas_state'] })
            }
          >
            <option value="">—</option>
            <option value="off">off</option>
            <option value="on">on</option>
          </select>
        </div>
        <div>
          <label className="text-muted-foreground">QAS scale</label>
          <input
            type="number"
            min={0}
            max={100}
            className="mt-0.5 w-full rounded border bg-background px-1.5 py-1 text-xs"
            placeholder="—"
            value={arm.qas_max_scale_factor}
            onChange={(e) => onChange({ qas_max_scale_factor: e.target.value })}
            disabled={arm.qas_state !== 'on'}
          />
        </div>
      </div>
    </div>
  )
}
