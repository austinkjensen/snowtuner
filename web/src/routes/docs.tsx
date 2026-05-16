import { createFileRoute } from '@tanstack/react-router'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  Terminal,
  Plug,
  FileCode,
} from 'lucide-react'
import { api, type CliCommand, type CliParam, type McpToolInfo } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'

export const Route = createFileRoute('/docs')({
  component: DocsPage,
})

// The FastAPI process is on :8770; the Vite dev server proxies /api/* to it
// but the auto-generated docs at /docs and /redoc only work when hit directly
// because Swagger loads its assets via relative URLs that don't survive the
// rewrite.  In production (single binary serving both) these will both be the
// same origin.
const API_ORIGIN =
  window.location.port === '5173'
    ? 'http://127.0.0.1:8770'
    : window.location.origin

function DocsPage() {
  return (
    <div className="mx-auto max-w-7xl px-6 py-6">
      <div className="mb-6 flex items-center gap-3">
        <BookOpen className="h-6 w-6 text-primary/80" />
        <h1 className="text-2xl font-semibold">Documentation</h1>
        <span className="text-sm text-muted-foreground">
          Auto-generated reference for the HTTP API, CLI, and MCP server.
        </span>
      </div>

      <div className="space-y-6">
        <ApiSection />
        <CliSection />
        <McpSection />
      </div>
    </div>
  )
}

// ── API section ─────────────────────────────────────────────────────

function ApiSection() {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <FileCode className="h-5 w-5" />
          HTTP API
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <p className="text-sm text-muted-foreground">
          The FastAPI service auto-generates interactive OpenAPI docs. Use these
          to inspect the exact request/response shape of any endpoint or send
          test requests against your local snowtuner.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button asChild variant="default" className="gap-1.5">
            <a href={`${API_ORIGIN}/docs`} target="_blank" rel="noreferrer">
              Swagger UI
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </Button>
          <Button asChild variant="outline" className="gap-1.5">
            <a href={`${API_ORIGIN}/redoc`} target="_blank" rel="noreferrer">
              ReDoc
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </Button>
          <Button asChild variant="ghost" className="gap-1.5">
            <a href={`${API_ORIGIN}/openapi.json`} target="_blank" rel="noreferrer">
              openapi.json
              <ExternalLink className="h-3.5 w-3.5" />
            </a>
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          API origin: <code className="font-mono">{API_ORIGIN}</code>
        </p>
      </CardContent>
    </Card>
  )
}

// ── CLI section ─────────────────────────────────────────────────────

function CliSection() {
  const tree = useQuery({
    queryKey: ['cli-help'],
    queryFn: api.cliHelp,
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Terminal className="h-5 w-5" />
          CLI ({tree.data?.subcommands.length ?? '…'} top-level commands)
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-4 text-sm text-muted-foreground">
          Run any command with <code className="font-mono">--help</code> for the
          same info in your terminal, e.g.{' '}
          <code className="font-mono">snowtuner experiments --help</code>.
        </p>
        {tree.isLoading && <div className="text-sm text-muted-foreground">Loading…</div>}
        {tree.data && (
          <div className="space-y-2">
            {tree.data.subcommands.map((c) => (
              <CommandNode key={c.name} cmd={c} depth={0} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function CommandNode({ cmd, depth }: { cmd: CliCommand; depth: number }) {
  const [open, setOpen] = useState(depth === 0 ? false : true)
  const indent = depth * 12
  const hasBody = cmd.params.length > 0 || cmd.subcommands.length > 0 || cmd.help
  const fullPath = cmd.path.join(' ')

  return (
    <div className="rounded-md border" style={{ marginLeft: indent }}>
      <button
        className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left hover:bg-muted/40"
        onClick={() => setOpen((v) => !v)}
        disabled={!hasBody}
      >
        {hasBody ? (
          open ? (
            <ChevronDown className="h-4 w-4 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-4 w-4 text-muted-foreground" />
          )
        ) : (
          <span className="w-4" />
        )}
        <code className="font-mono text-sm">{fullPath}</code>
        {cmd.is_group && (
          <Badge variant="secondary" className="text-xs">
            group
          </Badge>
        )}
        {cmd.short_help && !open && (
          <span className="ml-2 truncate text-xs text-muted-foreground">
            {cmd.short_help}
          </span>
        )}
      </button>

      {open && (
        <div className="space-y-3 border-t px-3 py-3">
          {cmd.help && (
            <pre className="whitespace-pre-wrap text-xs text-muted-foreground">
              {cmd.help}
            </pre>
          )}
          {cmd.params.length > 0 && (
            <ParamsTable params={cmd.params} />
          )}
          {cmd.subcommands.length > 0 && (
            <div className="space-y-2">
              {cmd.subcommands.map((sub) => (
                <CommandNode key={sub.name} cmd={sub} depth={depth + 1} />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function ParamsTable({ params }: { params: CliParam[] }) {
  const args = params.filter((p) => p.kind === 'argument')
  const opts = params.filter((p) => p.kind === 'option')
  return (
    <div className="space-y-2">
      {args.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
            Arguments
          </div>
          <table className="w-full text-xs">
            <tbody>
              {args.map((p) => (
                <tr key={p.name} className="border-b last:border-0">
                  <td className="py-1 pr-3 font-mono">{p.name}</td>
                  <td className="py-1 pr-3 text-muted-foreground">{p.type}</td>
                  <td className="py-1">
                    {p.required ? (
                      <Badge variant="outline" className="text-[10px]">
                        required
                      </Badge>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {opts.length > 0 && (
        <div>
          <div className="mb-1 text-xs font-semibold uppercase text-muted-foreground">
            Options
          </div>
          <table className="w-full text-xs">
            <tbody>
              {opts.map((p) => (
                <tr key={p.name} className="border-b align-top last:border-0">
                  <td className="py-1 pr-3 font-mono whitespace-nowrap">{p.name}</td>
                  <td className="py-1 pr-3 text-muted-foreground whitespace-nowrap">
                    {p.is_flag ? 'flag' : p.type}
                    {p.choices && (
                      <span className="ml-1 text-[10px]">
                        ({p.choices.join('|')})
                      </span>
                    )}
                  </td>
                  <td className="py-1 pr-3">{p.help}</td>
                  <td className="py-1 text-muted-foreground whitespace-nowrap">
                    {p.default ? `default: ${p.default}` : ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── MCP section ─────────────────────────────────────────────────────

function McpSection() {
  const tools = useQuery({
    queryKey: ['mcp-tools'],
    queryFn: api.mcpTools,
  })

  const groups = groupMcpTools(tools.data ?? [])

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Plug className="h-5 w-5" />
          MCP server ({tools.data?.length ?? '…'} tools)
        </CardTitle>
      </CardHeader>
      <CardContent>
        <p className="mb-4 text-sm text-muted-foreground">
          The admin MCP server forwards each tool call to this API. Wire it into
          Claude Desktop and your LLM agent can drive snowtuner end-to-end. See{' '}
          <code className="font-mono">snowtuner mcp --help</code> for the launch
          command and the README for Claude Desktop config.
        </p>
        {tools.isLoading && <div className="text-sm text-muted-foreground">Loading…</div>}
        {tools.data && (
          <div className="space-y-6">
            {Object.entries(groups).map(([groupName, items]) => (
              <div key={groupName}>
                <h3 className="mb-2 text-xs font-semibold uppercase text-muted-foreground">
                  {groupName} ({items.length})
                </h3>
                <div className="space-y-2">
                  {items.map((t) => (
                    <McpToolCard key={t.name} tool={t} />
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function groupMcpTools(tools: McpToolInfo[]): Record<string, McpToolInfo[]> {
  // Light heuristic — same grouping the README uses.
  const groups: Record<string, McpToolInfo[]> = {
    'Status / discovery': [],
    Recommendations: [],
    'Autonomous mode': [],
    Experiments: [],
    Other: [],
  }
  for (const t of tools) {
    if (/autonomous/.test(t.name)) groups['Autonomous mode'].push(t)
    else if (/experiment/.test(t.name)) groups['Experiments'].push(t)
    else if (/recommendation/.test(t.name)) groups['Recommendations'].push(t)
    else if (/status|warehouse|recommender/.test(t.name)) groups['Status / discovery'].push(t)
    else groups['Other'].push(t)
  }
  // Drop empty groups
  for (const k of Object.keys(groups)) {
    if (groups[k].length === 0) delete groups[k]
  }
  return groups
}

function McpToolCard({ tool }: { tool: McpToolInfo }) {
  const [showSchema, setShowSchema] = useState(false)
  const props =
    (tool.parameters as { properties?: Record<string, unknown> } | null)?.properties ?? null
  const propNames = props ? Object.keys(props) : []
  return (
    <div className="rounded-md border px-3 py-2">
      <div className="flex flex-wrap items-baseline gap-2">
        <code className="font-mono text-sm font-medium">{tool.name}</code>
        {propNames.length > 0 && (
          <span className="text-xs text-muted-foreground">
            ({propNames.join(', ')})
          </span>
        )}
      </div>
      {tool.description && (
        <p className="mt-1 whitespace-pre-wrap text-xs text-muted-foreground">
          {tool.description}
        </p>
      )}
      {tool.parameters && (
        <div className="mt-2">
          <button
            onClick={() => setShowSchema((v) => !v)}
            className="text-xs text-primary hover:underline"
          >
            {showSchema ? 'Hide' : 'Show'} JSON schema
          </button>
          {showSchema && (
            <pre className="mt-1 max-h-64 overflow-auto rounded bg-muted/40 p-2 text-[10px]">
              {JSON.stringify(tool.parameters, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}
