export const REPO = "PhaseYon/tacit";
export const REPO_URL = `https://github.com/${REPO}`;
export const repoPath = (path: string, ref = "main") => `${REPO_URL}/blob/${ref}/${path}`;
export const repoTree = (path: string, ref = "main") => `${REPO_URL}/tree/${ref}/${path}`;

// Not on PyPI yet, and the name "tacit" there belongs to an unrelated project, so install
// from GitHub. Switch to `uv tool install <package>` once a release is published.
export const INSTALL_CMD = `uv tool install git+${REPO_URL}`;
export const NEO4J_COMPOSE = repoPath("deploy/neo4j/docker-compose.yml");
export const DESIGN_DOC = repoPath("docs/design/local-first.md");
export const BENCHMARK_DIR = repoTree("benchmark");
export const DEMO_README = repoPath("demo/README.md");
