"""Graph layer (Graphiti on Neo4j). Only this package may import graphiti_core.

Env defaults must be set before graphiti_core is imported: it reads EMBEDDING_DIM at
import time, and its telemetry is on unless disabled.
"""

import os

os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")
os.environ.setdefault("EMBEDDING_DIM", "384")
