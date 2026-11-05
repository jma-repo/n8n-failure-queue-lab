"""Every relative link and anchor in the README and the docs must point at something that exists."""

import re

import pytest

from lab.paths import DOCS_DIR, ROOT

LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")
FILES = [ROOT / "README.md", *sorted(DOCS_DIR.glob("*.md"))]


def slug(heading: str) -> str:
    """GitHub's rule for heading anchors: lowercase, drop punctuation, spaces become hyphens."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    headings = re.findall(r"^#{1,6}\s+(.+)$", text, flags=re.MULTILINE)
    return {slug(h) for h in headings} | set(re.findall(r'<a id="([^"]+)"', text))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_relative_links_and_anchors_resolve(path):
    for target in LINK.findall(path.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        name, _, anchor = target.partition("#")
        dest = (path.parent / name).resolve() if name else path
        assert dest.exists(), f"{path.name}: {target} does not exist"
        if anchor and dest.suffix == ".md":
            assert anchor in anchors(dest), f"{path.name}: no heading or anchor {anchor} in {dest.name}"
