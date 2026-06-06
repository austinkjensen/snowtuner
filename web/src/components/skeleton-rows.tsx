/**
 * Loading placeholder used in list views while data is fetching.
 *
 * Renders ``count`` rows of pulsing bars; semantic wrapper is configurable
 * because some lists are ``<ul>`` (semantic list of items) and others are
 * ``<div>`` (visual grouping only).
 */

type SkeletonRowsProps = {
  /** Number of placeholder rows to render. */
  count?: number
  /** Semantic wrapper — ``ul`` for true lists, ``div`` for visual groupings. */
  as?: 'ul' | 'div'
  /** Vertical padding on each row.  Defaults to ``py-2.5`` (matches list views). */
  rowPadding?: string
}

export function SkeletonRows({
  count = 3,
  as = 'ul',
  rowPadding = 'py-2.5',
}: SkeletonRowsProps = {}) {
  const indices = Array.from({ length: count }, (_, i) => i)
  if (as === 'ul') {
    return (
      <ul className="divide-y divide-border">
        {indices.map((i) => (
          <li key={i} className={`px-3 ${rowPadding}`}>
            <div className="h-4 w-1/2 animate-pulse rounded bg-muted" />
          </li>
        ))}
      </ul>
    )
  }
  return (
    <div className="divide-y divide-border">
      {indices.map((i) => (
        <div key={i} className={`px-3 ${rowPadding}`}>
          <div className="h-4 w-1/2 animate-pulse rounded bg-muted" />
        </div>
      ))}
    </div>
  )
}
