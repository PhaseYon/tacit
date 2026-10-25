"""Only tacit_api/graph may import graphiti_core (design §3.4): the Neo4j credential can
read every group, so all access must go through graph/access.py's fail-closed path."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "tacit_api"


def test_graphiti_only_imported_in_graph_package():
    offenders = [
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "graph" not in p.relative_to(SRC).parts[:1]
        and re.search(r"^\s*(from|import)\s+graphiti_core", p.read_text(), re.M)
    ]
    assert offenders == []
