interface LoadingSkeletonProps {
  count?: number
}

function SkeletonCard() {
  return (
    <div className="glass-card rounded-2xl p-4 space-y-3 animate-pulse">
      <div className="flex items-center gap-3">
        <div className="w-8 h-8 rounded-xl bg-muted/60" />
        <div className="space-y-1.5 flex-1">
          <div className="h-4 bg-muted/70 rounded-md w-3/4" />
          <div className="h-3 bg-muted/40 rounded-md w-1/2" />
        </div>
      </div>
      <div className="flex gap-2 pt-1">
        <div className="h-6 bg-muted/50 rounded-lg w-20" />
        <div className="h-6 bg-muted/50 rounded-lg w-28" />
      </div>
    </div>
  )
}

export function LoadingSkeleton({ count = 3 }: LoadingSkeletonProps) {
  return (
    <div className="space-y-3">
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonCard key={i} />
      ))}
    </div>
  )
}
