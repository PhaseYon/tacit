export function PageSkeleton() {
  return (
    <div className="flex flex-1 flex-col" role="status" aria-label="Loading">
      <div className="h-14 border-b" />
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-10">
        <div className="h-8 w-48 animate-pulse rounded-lg bg-muted" />
        <div className="h-32 animate-pulse rounded-xl bg-muted" />
        <div className="h-24 animate-pulse rounded-xl bg-muted" />
      </div>
    </div>
  );
}
