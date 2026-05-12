import { createFileRoute, Link, useNavigate } from '@tanstack/react-router'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Beaker, Plus } from 'lucide-react'
import { api, type ExperimentStatus } from '@/lib/api'
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
                  <th className="px-4 py-2">Recipe</th>
                  <th className="px-4 py-2">Target</th>
                  <th className="px-4 py-2">Status</th>
                  <th className="px-4 py-2 text-right">Arms</th>
                  <th className="px-4 py-2">Cost estimate</th>
                  <th className="px-4 py-2">Best arm</th>
                  <th className="px-4 py-2">Proposed</th>
                </tr>
              </thead>
              <tbody>
                {list.data.map((e) => (
                  <tr key={e.id} className="border-b hover:bg-muted/30">
                    <td className="px-4 py-2 text-right font-mono text-xs">{e.id}</td>
                    <td className="px-4 py-2 font-medium">{e.proposed.recipe_name}</td>
                    <td className="px-4 py-2">
                      <Link
                        to="/warehouses/$name"
                        params={{ name: e.proposed.target_warehouse }}
                        className="text-primary hover:underline"
                      >
                        {e.proposed.target_warehouse}
                      </Link>
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
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
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

function NewExperimentDialog({
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <Card className="w-full max-w-lg">
        <CardHeader>
          <CardTitle>Propose a new experiment</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
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
            <label className="text-sm font-medium">Target warehouse</label>
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
        </CardContent>
      </Card>
    </div>
  )
}
