import { createFileRoute } from '@tanstack/react-router'
import { useQuery, useMutation } from '@tanstack/react-query'
import { AlertCircle, Check, KeyRound, RefreshCw, X } from 'lucide-react'
import { useState } from 'react'
import { api, getApiToken, setApiToken, type CredentialVerify } from '@/lib/api'
import { humanizeAgo } from '@/lib/format'
import { useTheme } from '@/components/theme-provider'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'

export const Route = createFileRoute('/settings')({
  component: SettingsPage,
})

function SettingsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Connection state, registered recommenders, and theme.
        </p>
      </div>

      <ApiAuthCard />
      <ConnectionCard />
      <RecommendersCard />
      <AppearanceCard />
    </div>
  )
}

// ── API auth ──────────────────────────────────────────────────────────────
// The snowtuner API may run in SNOWTUNER_AUTH_MODE=token, in which case
// the SPA needs to send a bearer token on every call.  This card stores
// the token in localStorage.  In SNOWTUNER_AUTH_MODE=none (local dev),
// the token field can stay empty.

function ApiAuthCard() {
  const [token, setToken] = useState(() => getApiToken() ?? '')
  const [saved, setSaved] = useState(false)
  function save() {
    setApiToken(token.trim() || null)
    setSaved(true)
    setTimeout(() => setSaved(false), 1500)
  }
  function clear() {
    setToken('')
    setApiToken(null)
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <KeyRound className="h-4 w-4" />
          API authentication
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-xs text-muted-foreground">
          When the API runs in token mode (
          <code className="text-xs">SNOWTUNER_AUTH_MODE=token</code>), every
          UI request needs a bearer token.  Get yours with{' '}
          <code className="text-xs">snowtuner auth show</code> and paste below.
          Stored in <code className="text-xs">localStorage</code> on this
          browser only.
        </p>
        <div className="flex gap-2">
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="Paste bearer token…"
            className="flex-1 rounded-md border bg-background px-3 py-1.5 text-sm font-mono"
          />
          <Button size="sm" onClick={save}>Save</Button>
          <Button size="sm" variant="outline" onClick={clear}>Clear</Button>
        </div>
        {saved && (
          <p className="text-xs text-green-600 flex items-center gap-1">
            <Check className="h-3 w-3" /> Token saved.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

// ── Connection ────────────────────────────────────────────────────────────

function ConnectionCard() {
  const creds = useQuery({ queryKey: ['credentials'], queryFn: api.credentials })
  const verify = useMutation<CredentialVerify>({
    mutationFn: api.verifyCredentials,
  })

  if (creds.isError) {
    return (
      <Card className="border-destructive/40">
        <CardHeader className="flex-row items-start gap-3 p-6">
          <AlertCircle className="h-5 w-5 text-destructive" />
          <div>
            <CardTitle className="text-foreground">Couldn't load credential state</CardTitle>
            <p className="mt-1 text-sm text-muted-foreground">
              Is <code className="font-mono">snowtuner api</code> reachable?
            </p>
          </div>
        </CardHeader>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between p-6 pb-2">
        <CardTitle>Connection</CardTitle>
        {creds.data?.configured && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => verify.mutate()}
            disabled={verify.isPending}
          >
            {verify.isPending ? (
              <>
                <RefreshCw className="h-3.5 w-3.5 animate-spin" /> Testing…
              </>
            ) : (
              <>
                <KeyRound className="h-3.5 w-3.5" /> Test connection
              </>
            )}
          </Button>
        )}
      </CardHeader>
      <CardContent className="p-6 pt-2">
        {creds.isLoading ? (
          <SkeletonRows />
        ) : !creds.data?.configured ? (
          <div className="rounded-md border border-warning/40 bg-warning/5 p-4 text-sm">
            <p className="font-medium text-foreground">No credentials configured.</p>
            <p className="mt-1 text-muted-foreground">
              Run <code className="font-mono">snowtuner init</code> in your terminal to set up a
              dedicated <code className="font-mono">SNOWTUNER_SVC</code> service user with RSA
              key-pair auth.
            </p>
          </div>
        ) : (
          <>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
              <Field label="Account">{creds.data.account}</Field>
              <Field label="User">{creds.data.user}</Field>
              <Field label="Role">{creds.data.role ?? '—'}</Field>
              <Field label="Default warehouse">{creds.data.warehouse ?? '—'}</Field>
              <Field label="Auth method">
                <code className="font-mono text-xs">{creds.data.auth_method}</code>
              </Field>
              <Field label="Source">
                <Badge variant="outline" className="font-mono text-xs">
                  {creds.data.source}
                </Badge>
              </Field>
              {creds.data.private_key_path && (
                <Field label="Private key">
                  <code className="font-mono text-xs">{creds.data.private_key_path}</code>
                </Field>
              )}
            </dl>

            {verify.data && <VerifyResult result={verify.data} />}
            {verify.isError && (
              <div className="mt-4 flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
                <X className="mt-0.5 h-4 w-4 text-destructive" />
                <p className="text-foreground">
                  {(verify.error as Error)?.message ?? 'Verify failed'}
                </p>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function VerifyResult({ result }: { result: CredentialVerify }) {
  if (!result.ok) {
    return (
      <div className="mt-4 flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
        <X className="mt-0.5 h-4 w-4 text-destructive" />
        <div>
          <p className="font-medium text-foreground">Connection failed</p>
          <p className="mt-1 font-mono text-xs text-muted-foreground">{result.error}</p>
        </div>
      </div>
    )
  }
  return (
    <div className="mt-4 flex items-start gap-2 rounded-md border border-success/40 bg-success/5 p-3 text-sm">
      <Check className="mt-0.5 h-4 w-4 text-success" />
      <div>
        <p className="font-medium text-foreground">Connected.</p>
        <p className="mt-1 text-xs text-muted-foreground">
          Snowflake reports: account <code className="font-mono">{result.account}</code> · user{' '}
          <code className="font-mono">{result.user}</code> · role{' '}
          <code className="font-mono">{result.role ?? '—'}</code> · warehouse{' '}
          <code className="font-mono">{result.warehouse ?? '—'}</code> · region{' '}
          <code className="font-mono">{result.region}</code>
        </p>
      </div>
    </div>
  )
}

// ── Recommenders ──────────────────────────────────────────────────────────

function RecommendersCard() {
  const recs = useQuery({ queryKey: ['recommenders'], queryFn: api.recommenders })
  const status = useQuery({ queryKey: ['status'], queryFn: api.status })

  const stateByName = new Map<string, { is_ready: boolean; last_fit_at: string | null; reason: string | null }>()
  for (const s of status.data?.recommender_states ?? []) {
    stateByName.set(s.name as string, {
      is_ready: Boolean(s.is_ready),
      last_fit_at: (s.last_fit_at as string | null) ?? null,
      reason: (s.reason as string | null) ?? null,
    })
  }

  return (
    <Card>
      <CardHeader className="p-6 pb-2">
        <CardTitle>Recommenders</CardTitle>
      </CardHeader>
      <CardContent className="p-3 pt-3">
        {recs.isLoading ? (
          <SkeletonRows />
        ) : recs.data?.length === 0 ? (
          <p className="px-3 py-6 text-center text-sm text-muted-foreground">
            No recommenders registered.
          </p>
        ) : (
          <ul className="divide-y divide-border">
            {(recs.data ?? []).map((r) => {
              const s = stateByName.get(r.name)
              return (
                <li key={r.name} className="flex flex-col gap-1 px-3 py-3 sm:flex-row sm:items-center">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 text-sm">
                      <span className="font-medium">{r.name}</span>
                      <span className="text-xs text-muted-foreground">v{r.version}</span>
                      <Badge variant="outline" className="font-mono text-[10px]">
                        {r.action_type}
                      </Badge>
                    </div>
                    {(() => {
                      const tables = r.required_feature_tables ?? []
                      if (tables.length === 0) return null
                      return (
                        <div className="mt-0.5 text-xs text-muted-foreground">
                          Reads:{' '}
                          {tables.map((t, i) => (
                            <span key={t}>
                              <code className="font-mono">{t}</code>
                              {i < tables.length - 1 && ', '}
                            </span>
                          ))}
                        </div>
                      )
                    })()}
                    {s?.reason && (
                      <p className="mt-0.5 text-xs text-muted-foreground">{s.reason}</p>
                    )}
                  </div>
                  <div className="flex shrink-0 items-center gap-3 text-xs">
                    {s ? (
                      s.is_ready ? (
                        <Badge variant="success">ready</Badge>
                      ) : (
                        <Badge variant="warning">training</Badge>
                      )
                    ) : (
                      <Badge variant="outline">untrained</Badge>
                    )}
                    <span className="tabular-nums text-muted-foreground">
                      last fit {humanizeAgo(s?.last_fit_at ?? null)}
                    </span>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

// ── Appearance ────────────────────────────────────────────────────────────

function AppearanceCard() {
  const { theme, setTheme, resolved } = useTheme()
  return (
    <Card>
      <CardHeader className="p-6 pb-2">
        <CardTitle>Appearance</CardTitle>
      </CardHeader>
      <CardContent className="p-6 pt-2 space-y-3">
        <div className="flex items-center gap-2">
          {(['dark', 'light', 'system'] as const).map((t) => (
            <Button
              key={t}
              variant={theme === t ? 'default' : 'outline'}
              size="sm"
              onClick={() => setTheme(t)}
            >
              {t}
            </Button>
          ))}
          <span className="ml-3 text-xs text-muted-foreground">
            currently rendering as <code className="font-mono">{resolved}</code>
          </span>
        </div>
        <p className="text-xs text-muted-foreground">
          Persisted to localStorage as <code className="font-mono">snowtuner-theme</code>.
          Default is dark.
        </p>
      </CardContent>
    </Card>
  )
}

// ── Shared bits ───────────────────────────────────────────────────────────

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-foreground">{children}</dd>
    </div>
  )
}

function SkeletonRows() {
  return (
    <div className="divide-y divide-border">
      {[0, 1, 2].map((i) => (
        <div key={i} className="px-3 py-3">
          <div className="h-4 w-1/2 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  )
}
