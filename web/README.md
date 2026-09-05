# snowtuner web UI

React + Vite single-page app for snowtuner. It talks to the FastAPI backend
over `/api/*` and renders recommendations, warehouses, the queries explorer,
experiments, and autonomous-mode controls.

## Running it in development

Start the backend first (`snowtuner api`, default `http://127.0.0.1:8770`).
Then, from this directory:

```bash
npm install
npm run dev        # Vite dev server on http://localhost:5173
```

The dev server proxies `/api/*` to `http://127.0.0.1:8770` (see
`vite.config.ts`), so the browser makes same-origin requests and there is no
CORS to configure. Open http://localhost:5173.

In production the API serves the built assets itself at `/` when
`SNOWTUNER_STATIC_DIR` points at `web/dist`, so there is no separate Vite
server. See [docs/aws-deploy.md](../docs/aws-deploy.md).

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server with HMR on :5173. |
| `npm run build` | Type-check (`tsc -b`), then build to `dist/`. |
| `npm run preview` | Serve the production build locally. |
| `npm run lint` | ESLint over the project. |
| `npm test` | Run the Vitest suite once. |
| `npm run test:watch` | Vitest in watch mode. |
| `npm run gen-types` | Regenerate `src/lib/api-types.ts` from the backend's `/openapi.json` (the API must be running). |

## Layout

- `src/lib/api.ts` - typed client over the FastAPI surface; requests use
  `BASE = '/api'` and attach the bearer token from `localStorage` when the API
  runs in `token` auth mode.
- `src/lib/api-types.ts` - generated from the backend OpenAPI schema. Do not
  edit by hand; run `npm run gen-types`.
- `src/routes/` - file-based routes (TanStack Router): recommendations,
  warehouses, queries, experiments, settings.
- `src/components/` - UI components, including the freshness pill in the top nav.

The stack is React, Vite, TanStack Router, and Tailwind with shadcn-style
primitives. See the [main README](../README.md) for the product overview and
[docs/architecture.md](../docs/architecture.md) for how the UI fits the rest of
the system.
