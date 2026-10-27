// The only questions the hands-on example asks. The route accepts these ids and nothing
// else, so visitors can't send arbitrary text to the demo database.
//
// `terms` is what the site's keyword search runs. The real CLI search also matches by
// meaning (local embeddings), so it doesn't need this; a serverless function can't
// carry the embedding model, so the showcase searches each question's key terms.
export const DEMO_QUESTIONS = [
  { id: "elasticsearch", question: "Why did we replace Elasticsearch?", terms: "Elasticsearch Postgres full-text search" },
  { id: "auth0", question: "Why did we move off Auth0?", terms: "Auth0 WorkOS" },
  { id: "heroku", question: "Why did we leave Heroku?", terms: "Heroku AWS" },
  { id: "rate-limit", question: "How do we rate limit the API?", terms: "rate limit" },
] as const;

export type DemoQuestionId = (typeof DEMO_QUESTIONS)[number]["id"];

export function questionFor(id: unknown) {
  return DEMO_QUESTIONS.find((q) => q.id === id) ?? null;
}
