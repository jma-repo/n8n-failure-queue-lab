"""Where things live. `LAB_ROOT` overrides the repository root (the container sets it)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("LAB_ROOT") or Path(__file__).resolve().parents[2])
CONFIG_DIR = ROOT / "config"
SQL_DIR = ROOT / "sql"
N8N_DIR = ROOT / "n8n"
DOCS_DIR = ROOT / "docs"
