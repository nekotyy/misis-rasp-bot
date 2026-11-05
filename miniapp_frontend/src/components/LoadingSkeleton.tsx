interface LoadingSkeletonProps {
  count?: number
}

function SkeletonCard() {
  return (
    <div className="rounded-xl border border-border p-3 space-y-2 animate-pulse">
      <div className="flex items-center gap-2">
        <div className="w-7 h-7 rounded-full bg-muted" />
        <div className="h-4 bg-muted rounded w-2/3" />
      </div>
      <div className="flex gap-4">
        <div className="h-3 bg-muted rounded w-1/4" />
        <div className="h-3 bg-muted rounded w-1/5" />
      </div>
    </div>
  )
}

export function LoadingSkeleton({ count = 4 }: LoadingSkeletonProps) {
  return (
    <div className="space-y-2">
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonCard key={i} />
      ))}
    </div>
  )
}
