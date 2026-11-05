"""Rebuild n8n/workflows/*.json: uv run python scripts/build_workflows.py [--check]"""

import sys

from lab.workflows.build import main

if __name__ == "__main__":
    sys.exit(main())
