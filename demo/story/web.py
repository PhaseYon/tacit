"""larkspur/web: the private Next.js web app and offline PWA."""

from .model import PR, Doc, Issue, Repo, approve, c, comment_review, rc

HB = "https://github.com/larkspur/handbook/blob/main/docs/adr"

README = f"""
# larkspur/web

The Larkspur web app and offline-first PWA used by inspectors in the field.

- Next.js (App Router), TypeScript
- Server state: TanStack Query, persisted to IndexedDB; client state: Zustand
  ([ADR-0011]({HB}/0011-tanstack-query-for-server-state.md))
- Offline: service worker + IndexedDB drafts + durable photo outbox
  ([ADR-0008]({HB}/0008-offline-first-pwa.md))
- Auth: WorkOS AuthKit
- `/print/reports/[id]` is the print layout the API renders to PDF with Playwright

```bash
pnpm install
pnpm dev
```
"""

DOCS = [Doc("README.md", README, key="readme")]

ITEMS: list[PR | Issue] = [
    PR(
        "pr-skeleton",
        "Next.js app skeleton",
        "sam-whitfield",
        "2025-04-02 10:00",
        "App Router, TypeScript strict, ESLint, Vitest, Playwright for e2e. Nothing product-facing yet.",
        merged="2025-04-03 10:00",
        labels=["chore"],
        reviews=[approve("maya-chen", "2025-04-03 09:00")],
    ),
    PR(
        "pr-redux-store",
        "Redux Toolkit store with report, client and template slices",
        "sam-whitfield",
        "2025-05-06 10:00",
        """
Per ADR-0006. One slice per domain, thunks for API calls, `createEntityAdapter` for
normalized reports. Report editor undo uses a history middleware on the `report` slice.
""",
        merged="2025-05-08 10:00",
        labels=["frontend"],
        reviews=[
            approve(
                "maya-chen",
                "2025-05-08 09:00",
                "Approved. The loading/error boilerplate per thunk is a lot, but consistent.",
                key="redux-store-maya",
            ),
        ],
    ),
    PR(
        "pr-auth0-login",
        "Login with Auth0 (nextjs-auth0)",
        "sam-whitfield",
        "2025-05-19 10:00",
        "Universal Login redirect, access token passed to the API as a Bearer token.",
        merged="2025-05-21 10:00",
        labels=["auth"],
        reviews=[approve("jonah-blake", "2025-05-21 09:00")],
    ),
    PR(
        "pr-search-ui",
        "Search page with highlighted results",
        "sam-whitfield",
        "2025-05-28 10:00",
        "Uses `GET /search` (Elasticsearch-backed) and renders ES `highlight` fragments. Debounced 250 ms.",
        merged="2025-05-30 10:00",
        labels=["frontend", "search"],
        reviews=[approve("jonah-blake", "2025-05-30 09:00")],
    ),
    PR(
        "pr-v1-client",
        "Call /v1 API paths",
        "sam-whitfield",
        "2025-06-19 10:00",
        "Matches the API change for ADR-0007.",
        merged="2025-06-20 10:30",
        labels=["api"],
        reviews=[approve("tomas-rivera", "2025-06-20 10:00")],
    ),
    PR(
        "pr-autosave",
        "Report editor autosave",
        "sam-whitfield",
        "2025-06-23 10:00",
        "Saves the report 2 s after the last edit. Sends `If-Match` with the report version to detect conflicts.",
        merged="2025-06-26 10:00",
        labels=["frontend"],
        reviews=[
            comment_review(
                "lena-park",
                "2025-06-24 10:00",
                "",
                comments=[
                    rc(
                        "src/editor/useAutosave.ts",
                        40,
                        "On a 412 we throw away local edits. In the field that means losing "
                        "a finding someone just typed. Can we keep them and show a conflict "
                        "banner instead?",
                        key="autosave-412",
                    )
                ],
            ),
            approve("lena-park", "2025-06-25 15:00", "Thanks for the banner. Approve."),
        ],
    ),
    PR(
        "pr-offline-sw",
        "Offline-first: service worker, IndexedDB drafts and photo outbox (ADR-0008)",
        "lena-park",
        "2025-08-12 10:00",
        """
Implements ADR-0008.

- Workbox service worker precaches the app shell and caches `/v1` GETs (stale-while-revalidate)
- Report drafts live in IndexedDB; edits are queued in an **outbox** and replayed in order
- Photos are written to the outbox as Blobs and uploaded in the background, with retry
- Background Sync where supported; otherwise sync on app open/focus (iOS)
""",
        merged="2025-08-20 10:00",
        labels=["frontend", "mobile", "offline"],
        reviews=[
            comment_review(
                "sam-whitfield",
                "2025-08-13 11:00",
                "",
                comments=[
                    rc(
                        "src/offline/outbox.ts",
                        77,
                        "If the tab is killed between `upload()` resolving and `delete(entry)`, "
                        "we re-upload the photo on next open. Can uploads be idempotent?",
                        key="outbox-idempotent",
                    )
                ],
            ),
            comment_review(
                "lena-park",
                "2025-08-14 10:00",
                "",
                comments=[
                    rc(
                        "src/offline/outbox.ts",
                        77,
                        "Each outbox entry gets a client-generated id that the API dedupes on. "
                        "Priya is proposing UUIDv7 for exactly this case.",
                        reply_to="outbox-idempotent",
                    )
                ],
            ),
            approve(
                "sam-whitfield",
                "2025-08-19 16:00",
                "Approve. Tested in airplane mode on iPhone and Android: 120 photos queued, all "
                "uploaded after reconnect.",
            ),
        ],
        comments=[
            c(
                "tomas-rivera",
                "2025-08-15 09:00",
                "API side: `PUT /v1/photos/{client_id}` is idempotent now, so retries are safe.",
            ),
        ],
    ),
    Issue(
        "issue-ios-quota",
        "iOS evicts offline photos when storage is low",
        "lena-park",
        "2025-09-01 10:00",
        """
A pilot inspector lost 30 photos: iOS Safari evicted our IndexedDB data after 7 days of
not opening the app, and the outbox had unsent full-resolution photos (8 MB each).
""",
        closed="2025-09-10 12:00",
        state_reason="completed",
        labels=["bug", "mobile", "offline"],
        comments=[
            c(
                "sam-whitfield",
                "2025-09-01 13:00",
                "`navigator.storage.persist()` is ignored for non-installed PWAs on iOS. We "
                "should prompt users to 'Add to Home Screen' and compress photos before "
                "queueing.",
            ),
            c(
                "lena-park",
                "2025-09-02 09:00",
                "Compressing to 2048px JPEG q0.8 brings photos to ~600 KB. Doing it in "
                "{#pr-photo-compress}. ADR-0008 already lists this as a known risk.",
            ),
            c(
                "lena-park",
                "2025-09-10 12:00",
                "Shipped, plus an install prompt after the first inspection.",
            ),
        ],
    ),
    PR(
        "pr-photo-compress",
        "Compress photos client-side before queueing",
        "lena-park",
        "2025-09-08 10:00",
        "Fixes {#issue-ios-quota}. Resize to 2048px, JPEG q0.8, in a Web Worker so the UI doesn't stall. Originals are not kept on device.",
        merged="2025-09-10 11:00",
        labels=["mobile", "offline"],
        reviews=[approve("sam-whitfield", "2025-09-09 15:00")],
    ),
    Issue(
        "issue-redux-boilerplate",
        "Redux boilerplate is slowing us down",
        "lena-park",
        "2025-10-20 10:00",
        """
Adding the client-history screen took a slice, 4 thunks, 6 selectors and the
loading/error/stale flags, plus cache invalidation when a report changes. ~80% of the store
is just cached server data. And replaying thunks from the offline outbox is fragile.

Proposal: TanStack Query for server state, something tiny (Zustand) for UI state.
""",
        closed="2026-02-10 12:00",
        state_reason="completed",
        labels=["frontend", "discussion"],
        comments=[
            c(
                "sam-whitfield",
                "2025-10-21 10:00",
                "I picked Redux in ADR-0006 because of the editor's nested state and undo. That "
                "still matters, but it's maybe 10% of the store now. I'm open to it; let's do a "
                "spike on the reports list.",
                key="redux-sam",
            ),
            c(
                "maya-chen",
                "2025-10-22 10:00",
                "Fine by me if it's a frontend-only change. Write a new ADR that supersedes "
                "ADR-0006 rather than editing it.",
            ),
            c(
                "lena-park",
                "2026-02-10 12:00",
                "ADR-0011 merged. Migration tracked in {#pr-tanstack-reports} and {#pr-remove-redux}.",
            ),
        ],
    ),
    PR(
        "pr-dependabot-next",
        "Bump next from 15.0.3 to 15.0.4",
        "dependabot[bot]",
        "2025-11-03 06:00",
        "Bumps [next](https://github.com/vercel/next.js) from 15.0.3 to 15.0.4.",
        merged="2025-11-03 10:00",
        labels=["dependencies"],
        branch="dependabot/npm_and_yarn/next-15.0.4",
        reviews=[approve("sam-whitfield", "2025-11-03 09:50")],
        comments=[
            c("github-actions[bot]", "2025-11-03 06:10", "✅ Lint, typecheck and 318 tests passed.")
        ],
    ),
    Issue(
        "issue-bundle-size",
        "Initial JS bundle is 1.1 MB",
        "sam-whitfield",
        "2025-11-20 10:00",
        "Lighthouse on a mid-range Android: 6.2 s TTI. The PDF preview library and moment.js are in the main chunk.",
        closed="2025-11-28 10:00",
        state_reason="completed",
        labels=["performance"],
        comments=[
            c(
                "lena-park",
                "2025-11-21 10:00",
                "Swapping moment for date-fns and lazy-loading the preview gets us to 640 KB.",
            ),
            c("sam-whitfield", "2025-11-28 10:00", "Done, TTI 3.4 s."),
        ],
    ),
    Issue(
        "issue-safari-bg-sync",
        "Safari: Background Sync never fires",
        "lena-park",
        "2025-12-08 10:00",
        "As expected, Safari has no Background Sync API. Outbox only drains when the app is open. Tracking here in case WebKit ships it.",
        labels=["mobile", "offline", "upstream"],
        comments=[
            c(
                "sam-whitfield",
                "2025-12-08 14:00",
                "Foreground sync on focus is our workaround (ADR-0008). Leaving open.",
            )
        ],
    ),
    Issue(
        "issue-print-route",
        "Print layout route for server-side PDF rendering",
        "tomas-rivera",
        "2026-01-08 10:00",
        "For the Playwright switch the PDF worker will open a page in the web app. Can we get `/print/reports/[id]` that renders the report without app chrome, authenticated by a one-time token?",
        closed="2026-01-15 12:00",
        state_reason="completed",
        labels=["pdf"],
        comments=[
            c(
                "sam-whitfield",
                "2026-01-08 13:00",
                "Yes. I'll reuse the report view components with print CSS, CSS grid for photos.",
            ),
            c("tomas-rivera", "2026-01-15 12:00", "Works with the worker on staging. Thanks!"),
        ],
    ),
    PR(
        "pr-print-route",
        "Add /print/reports/[id] for PDF rendering",
        "sam-whitfield",
        "2026-01-12 10:00",
        "Closes {#issue-print-route}. Print CSS with `@page` margins, photo grid (3 per row), page-break rules for findings. Token verified server-side, single use, 5-minute TTL.",
        merged="2026-01-15 10:00",
        labels=["pdf", "frontend"],
        reviews=[approve("tomas-rivera", "2026-01-14 16:00")],
    ),
    PR(
        "pr-tanstack-spike",
        "Spike: reports list on TanStack Query",
        "lena-park",
        "2026-01-26 10:00",
        """
Spike for {#issue-redux-boilerplate}. The reports list and report detail on TanStack Query
with the cache persisted to IndexedDB. Removes the `reports` slice and its thunks:
-612 / +140 lines. Offline: cached lists render instantly in airplane mode.
""",
        closed="2026-02-12 10:00",
        labels=["frontend", "spike"],
        comments=[
            c(
                "sam-whitfield",
                "2026-01-28 10:00",
                "Convincing. Write the ADR and we'll do it properly.",
            ),
        ],
    ),
    PR(
        "pr-tanstack-reports",
        "TanStack Query for reports list and detail (ADR-0011)",
        "lena-park",
        "2026-02-12 11:00",
        "First migration step from ADR-0011. `QueryClient` with an IndexedDB persister; `reports` slice removed.",
        merged="2026-02-17 10:00",
        labels=["frontend"],
        reviews=[approve("sam-whitfield", "2026-02-16 15:00")],
    ),
    PR(
        "pr-remove-redux",
        "Migrate remaining slices to TanStack Query/Zustand and remove Redux",
        "lena-park",
        "2026-03-02 10:00",
        """
Completes ADR-0011.

- clients, templates → TanStack Query
- editor UI + undo history → Zustand store with a small history middleware
- outbox status → Zustand, persisted separately (not in the query cache, per Maya's note)
- `@reduxjs/toolkit` and `react-redux` removed: -2.3k lines, bundle -38 KB
""",
        merged="2026-03-06 10:00",
        labels=["frontend"],
        reviews=[
            comment_review(
                "sam-whitfield",
                "2026-03-03 10:00",
                "",
                comments=[
                    rc(
                        "src/editor/store.ts",
                        15,
                        "Undo history capped at 50 steps? Redux version had 100.",
                        key="zustand-undo",
                    )
                ],
            ),
            comment_review(
                "lena-park",
                "2026-03-03 14:00",
                "",
                comments=[rc("src/editor/store.ts", 15, "Bumped to 100.", reply_to="zustand-undo")],
            ),
            approve("sam-whitfield", "2026-03-05 16:00", "Approve. Goodbye Redux 👋"),
        ],
    ),
    PR(
        "pr-search-highlights",
        "Search: render ts_headline highlights from the Postgres backend",
        "sam-whitfield",
        "2026-03-24 10:00",
        "The API now returns `highlights` from `ts_headline` (`<b>` tags) instead of ES fragments. Sanitize and render.",
        merged="2026-03-25 10:00",
        labels=["search", "frontend"],
        reviews=[approve("priya-natarajan", "2026-03-24 16:00")],
    ),
    Issue(
        "issue-search-empty-state",
        "Search empty state says 'Elasticsearch error'",
        "sam-whitfield",
        "2026-04-01 10:00",
        "Leftover copy from the ES days. Should be a friendly 'No reports match'.",
        closed="2026-04-02 10:00",
        state_reason="completed",
        labels=["frontend"],
        comments=[c("sam-whitfield", "2026-04-02 10:00", "Fixed.")],
    ),
    Issue(
        "issue-native-app",
        "Customer request: native iOS app",
        "ines-duarte",
        "2026-04-20 10:00",
        """
Harbor & Pine Inspections (35 seats, renewal in June) asked for a native iOS app. Their
reasons: App Store presence for their inspectors and better photo uploads in the
background. Do we revisit ADR-0008?
""",
        closed="2026-04-28 12:00",
        state_reason="not_planned",
        labels=["mobile", "product"],
        comments=[
            c(
                "lena-park",
                "2026-04-21 10:00",
                "Background upload is the one real gap (no Background Sync on iOS). Since photo "
                "compression shipped, we've had zero lost-photo tickets in six months. A React "
                "Native app is ~4 months for two of us plus ongoing app store releases.",
                key="native-lena",
            ),
            c(
                "sam-whitfield",
                "2026-04-21 14:00",
                "We could add a 'keep app open while syncing' indicator and push to finish "
                "uploads. That covers most of their complaint.",
            ),
            c(
                "maya-chen",
                "2026-04-22 10:00",
                "The ADR-0008 revisit triggers were MDM distribution or real photo loss. "
                "Neither applies. I'd stay PWA, but it's a product call.",
            ),
            c(
                "ines-duarte",
                "2026-04-28 11:55",
                "Decision: **no native app this year**. We stay with the PWA per ADR-0008, ship "
                "Sam's sync indicator, and I'll talk to Harbor & Pine. We revisit when we reach "
                "50 enterprise seats on one customer or someone requires MDM. Closing as not "
                "planned.",
                key="native-ines-decision",
            ),
        ],
    ),
    Issue(
        "issue-a11y",
        "Accessibility audit findings",
        "lena-park",
        "2026-05-05 10:00",
        "Audit for an enterprise RFP found: missing labels on checklist toggles, low contrast on the severity chips, and focus lost after closing the photo viewer.",
        closed="2026-05-19 10:00",
        state_reason="completed",
        labels=["accessibility"],
        comments=[
            c(
                "sam-whitfield",
                "2026-05-06 10:00",
                "I'll take the focus management, Lena the labels and contrast?",
            ),
            c(
                "lena-park",
                "2026-05-19 10:00",
                "All fixed, axe reports 0 violations on the editor.",
            ),
        ],
    ),
    PR(
        "pr-workos-login",
        "Switch login to WorkOS AuthKit",
        "sam-whitfield",
        "2026-06-03 10:00",
        "Replaces nextjs-auth0 with WorkOS AuthKit. SSO users are routed to their IdP by email domain. Sessions in an httpOnly cookie.",
        merged="2026-06-09 08:00",
        labels=["auth"],
        reviews=[
            approve(
                "ravi-kapoor",
                "2026-06-08 15:00",
                "Approve. Cookie is httpOnly, Secure, SameSite=Lax; logout revokes the WorkOS "
                "session.",
            ),
        ],
    ),
    Issue(
        "issue-login-loop",
        "Redirect loop on login for Brightwell SSO users",
        "sam-whitfield",
        "2026-06-10 09:00",
        "Brightwell users bounce between /login and their Entra ID page after the WorkOS switch.",
        closed="2026-06-10 15:00",
        state_reason="completed",
        labels=["bug", "auth"],
        comments=[
            c(
                "ravi-kapoor",
                "2026-06-10 10:00",
                "Their domain wasn't verified in WorkOS, so AuthKit fell back to password login, which then redirected to SSO. Verified the domain.",
            ),
            c("sam-whitfield", "2026-06-10 15:00", "Confirmed fixed with their admin."),
        ],
    ),
    Issue(
        "issue-offline-conflict",
        "Two inspectors editing the same report offline overwrite each other",
        "lena-park",
        "2026-06-15 10:00",
        """
Firms with two-person inspections (one outside, one inside) both edit the same report
offline. On sync, whoever syncs last overwrites whole sections.
""",
        closed="2026-07-06 12:00",
        state_reason="completed",
        labels=["offline", "bug"],
        comments=[
            c(
                "sam-whitfield",
                "2026-06-15 14:00",
                "CRDTs (Yjs) would solve it properly but it's a big change to the editor and the "
                "API. Field-level last-writer-wins would cover 95% of cases, since the two "
                "people edit different sections.",
            ),
            c(
                "maya-chen",
                "2026-06-16 10:00",
                "Go with per-field last-writer-wins plus a conflict banner when the same field "
                "was edited by both. Revisit CRDTs only if this keeps coming up.",
                key="offline-conflict-maya",
            ),
            c("lena-park", "2026-07-06 12:00", "Shipped in {#pr-field-merge}."),
        ],
    ),
    PR(
        "pr-field-merge",
        "Per-field last-writer-wins merge for offline edits",
        "lena-park",
        "2026-07-01 10:00",
        "Fixes {#issue-offline-conflict}. Outbox entries carry field paths and timestamps; the API merges per field. A banner shows when both inspectors changed the same field.",
        merged="2026-07-06 10:00",
        labels=["offline"],
        reviews=[approve("sam-whitfield", "2026-07-03 15:00")],
        comments=[
            c(
                "tomas-rivera",
                "2026-07-02 10:00",
                "API merge endpoint is in larkspur/api, deployed to staging.",
            )
        ],
    ),
    PR(
        "pr-dark-mode",
        "Dark mode",
        "lena-park",
        "2026-07-07 10:00",
        "Follows the OS setting, with a toggle in settings. Report PDFs stay light.",
        merged="2026-07-10 10:00",
        labels=["frontend"],
        reviews=[
            approve(
                "sam-whitfield",
                "2026-07-09 15:00",
                "Looks great. Crawl-space inspectors will thank you.",
            )
        ],
        comments=[c("kofi-mensah", "2026-07-08 09:00", "Finally 🌙")],
    ),
    Issue(
        "issue-photo-rotation",
        "Photos from some Android phones appear sideways",
        "lena-park",
        "2026-08-03 10:00",
        "Client-side compression drops EXIF orientation on Samsung devices.",
        closed="2026-08-05 10:00",
        state_reason="completed",
        labels=["bug", "mobile"],
        comments=[
            c(
                "lena-park",
                "2026-08-05 10:00",
                "Fixed: apply orientation before resizing with `createImageBitmap(..., {imageOrientation: 'from-image'})`.",
            )
        ],
    ),
    PR(
        "pr-dependabot-tanstack",
        "Bump @tanstack/react-query from 5.62.0 to 5.66.0",
        "dependabot[bot]",
        "2026-09-07 06:00",
        "Bumps [@tanstack/react-query](https://github.com/TanStack/query) from 5.62.0 to 5.66.0.",
        labels=["dependencies"],
        branch="dependabot/npm_and_yarn/tanstack/react-query-5.66.0",
        comments=[
            c("github-actions[bot]", "2026-09-07 06:12", "✅ Lint, typecheck and 402 tests passed.")
        ],
    ),
    PR(
        "pr-comparison-draft",
        "WIP: compare two inspections of the same property",
        "sam-whitfield",
        "2026-09-22 10:00",
        "Early draft of a side-by-side view for re-inspections. Not ready.",
        draft=True,
        labels=["frontend"],
    ),
]

REPO = Repo(
    name="web",
    private=True,
    description="Larkspur web app and offline-first PWA",
    created="2025-04-01 12:00",
    items=ITEMS,
    docs=DOCS,
)
