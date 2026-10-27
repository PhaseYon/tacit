export type DemoResult = {
  title: string;
  url: string;
  source: string;
  kind: string;
  repo?: string | null;
  date?: string | null;
  snippet: string;
};

export type DemoResponse =
  | {
      mode: "live";
      query: string;
      search: "keyword";
      results: DemoResult[];
      tookMs: number;
    }
  | {
      mode: "example";
      query: string;
      // Why the live database wasn't used.
      reason: "not_configured" | "unavailable";
      // The recorded example query whose results are shown.
      exampleQuery: string;
      results: DemoResult[];
    };

export type DemoError = { error: string; retryAfter?: number };

export const MAX_QUERY_CHARS = 200;
