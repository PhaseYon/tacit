export const REPO = "PhaseYon/tacit";
export const REPO_URL = `https://github.com/${REPO}`;
export const repoPath = (path: string, ref = "main") => `${REPO_URL}/blob/${ref}/${path}`;
export const repoTree = (path: string, ref = "main") => `${REPO_URL}/tree/${ref}/${path}`;

export const INSTALL_CMD = "uvx tacit init";
export const NEO4J_COMPOSE = repoPath("deploy/neo4j/docker-compose.yml");
export const DESIGN_DOC = repoPath("docs/design/local-first.md");
export const BENCHMARK_DIR = repoTree("benchmark");
export const DEMO_README = repoPath("demo/README.md");
