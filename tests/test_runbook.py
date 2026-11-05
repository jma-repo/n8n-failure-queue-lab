"""The runbook, the taxonomy and the retry numbers quoted in the playbooks must agree."""

import re

import pytest

from lab.paths import DOCS_DIR
from lab.taxonomy import load_taxonomy

RUNBOOK = (DOCS_DIR / "runbook.md").read_text(encoding="utf-8")
TAXONOMY = load_taxonomy()


def section(anchor: str) -> str:
    start = RUNBOOK.index(f'<a id="{anchor}"></a>')
    nxt = RUNBOOK.find('<a id="', start + 10)
    end = nxt if nxt != -1 else RUNBOOK.index("## 6.")
    return RUNBOOK[start:end]


@pytest.mark.parametrize("name", list(TAXONOMY))
def test_every_error_type_has_a_playbook_with_its_own_name_in_the_title(name):
    body = section(TAXONOMY[name].runbook)
    assert f"### {name}:" in body


def test_every_playbook_belongs_to_an_error_type():
    anchors = set(re.findall(r'<a id="([a-z0-9-]+)"></a>', RUNBOOK))
    assert anchors == {t.runbook for t in TAXONOMY.values()}


@pytest.mark.parametrize("name", [n for n, t in TAXONOMY.items() if t.retryable])
def test_the_retry_budget_quoted_in_a_playbook_is_the_real_one(name):
    t = TAXONOMY[name]
    quoted = re.search(r"(\d+) retries", section(t.runbook))
    assert quoted and int(quoted.group(1)) == t.max_attempts, name


@pytest.mark.parametrize("name", [n for n, t in TAXONOMY.items() if not t.retryable])
def test_a_non_retryable_type_says_it_is_dead_on_arrival(name):
    assert "dead-lettered on arrival" in section(TAXONOMY[name].runbook)
