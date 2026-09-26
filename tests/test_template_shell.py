"""Every page with the shared navigation gets the responsive app shell.

dashboard/static/app.js only activates the shell (mobile drawer, collapsed
rail) when the page has ``<main class="page ...">``. Pages that used
``page-shell`` alone overflowed a 390px viewport by ~1100px (found by
scripts/browser_acceptance.mjs).
"""

import re
from pathlib import Path

import pytest


TEMPLATES = Path(__file__).resolve().parents[1] / "dashboard" / "templates"
PAGES = sorted(
    path for path in TEMPLATES.glob("*.html")
    if not path.name.startswith("_") and '{% include "_nav.html" %}' in path.read_text(encoding="utf-8")
)


def test_pages_with_navigation_exist():
    assert len(PAGES) > 20


@pytest.mark.parametrize("template", PAGES, ids=lambda path: path.name)
def test_page_main_element_activates_the_shell(template):
    main = re.search(r"<main\b[^>]*>", template.read_text(encoding="utf-8"))
    assert main, f"{template.name} has no <main> element"
    classes = re.search(r'class="([^"]*)"', main.group(0))
    tokens = re.sub(r"\{[%{].*?[%}]\}", " ", classes.group(1) if classes else "").split()
    assert "page" in tokens, f"{template.name}: <main> needs class 'page' for the app shell"
