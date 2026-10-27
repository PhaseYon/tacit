// TODO(recording): replace this placeholder with a screen recording of Claude Desktop
// answering "Why did we replace Elasticsearch?" over the demo graph, with citations.
// Plan: an MP4/WebM in public/ (no third-party embed, so no tracking), rendered as
//   <video controls preload="metadata" poster="/recording-poster.png" src="/recording.mp4">
// with captions (<track kind="captions">) and a short text transcript below it.

export function Recording() {
  return (
    <div
      className="relative grid aspect-video w-full place-items-center overflow-hidden rounded-2xl border border-dashed border-border-strong bg-surface-2"
      role="img"
      aria-label="Placeholder: a screen recording of Claude Desktop answering a question with Tacit is coming soon."
    >
      <div className="flex flex-col items-center gap-3 px-6 text-center">
        <span className="grid size-12 place-items-center rounded-full bg-surface text-muted shadow-sm">
          <svg aria-hidden width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
            <path d="M8 5.5v13l10.5-6.5L8 5.5Z" />
          </svg>
        </span>
        <p className="font-medium">Recording coming soon</p>
        <p className="max-w-md text-sm text-muted">
          Claude Desktop answering &ldquo;Why did we replace Elasticsearch?&rdquo; from the demo
          graph, citing the ADR, the incident and the postmortem.
        </p>
      </div>
    </div>
  );
}
