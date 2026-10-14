import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "web" / "static" / "styles.css").read_text(encoding="utf-8")
JS = (ROOT / "web" / "static" / "app.js").read_text(encoding="utf-8")


def test_every_existing_id_is_kept():
    frozen = (ROOT / "tests" / "fixtures" / "dashboard_ids.txt").read_text(encoding="utf-8").split()
    present = set(re.findall(r'\bid="([^"]+)"', HTML))
    assert [i for i in frozen if i not in present] == []


def test_app_shell_exists():
    for marker in ('class="app-shell"', 'id="sidebar"', 'class="topbar"', 'id="sidebar-toggle"',
                   'id="page-title"', 'class="sidebar-backdrop"'):
        assert marker in HTML, marker


def test_drawer_is_wired_in_js():
    assert "sidebar-open" in JS
    assert "Escape" in JS
    assert "page-title" in JS


def test_old_floating_bottom_bar_is_gone():
    assert "Rail collapses to a bottom bar" not in CSS


def test_labels_are_not_forced_uppercase():
    assert "text-transform: uppercase" not in CSS


def test_page_root_does_not_break_sticky_sidebar():
    # overflow-x: hidden on html/body makes body a scroll container, so the
    # sticky sidebar/top bar scrolled away with the page; clip does not.
    for selector in ("html {", "body {"):
        block = CSS[CSS.index(selector):CSS.index("}", CSS.index(selector))]
        assert "overflow-x: hidden" not in block, selector


def test_no_rules_for_the_removed_header_layout():
    # Bare `header` selectors from the old boxed header leaked onto the new
    # top bar (rounded corners, hidden language picker).
    assert re.search(r"(?m)^\s*header\b", CSS) is None
    for dead in (".header-meta", ".header-brand {"):
        assert dead not in CSS, dead


def test_help_links_close_the_drawer():
    handler = JS[JS.index("document.querySelectorAll('.tabs-subnav-link').forEach"):]
    assert "closeSidebar()" in handler[:400]


def test_phone_uses_a_bottom_navigation_bar():
    # On phones the five sections live in a fixed bottom bar, not in a drawer;
    # the drawer is only for switching accounts there.
    assert 'id="bottom-nav"' in HTML
    assert "placeNav" in JS and "(max-width: 768px)" in JS
    assert ".bottom-nav" in CSS
