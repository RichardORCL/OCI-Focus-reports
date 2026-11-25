"""
Reusable top menu bar for FOCUS report pages.
Provides navigation to Home, Monthly reports, Storage (unattached volumes), and Per-user dashboard.
"""

from __future__ import annotations
import html
import re
from pathlib import Path

# SVG icons for the top menu
NAV_HOME_ICON = '''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>'''
NAV_CALENDAR_ICON = '''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2" ry="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>'''
NAV_USERS_ICON = '''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>'''
NAV_STORAGE_ICON = '''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="12" x2="2" y2="12"/><path d="M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><line x1="6" y1="16" x2="6.01" y2="16"/><line x1="10" y1="16" x2="10.01" y2="16"/></svg>'''


def find_latest_monthly_report(consolidated_dir: Path) -> str | None:
    """
    Find the path to the latest monthly report (YYYY/YYYY-MM.html).
    Returns relative path like "2026/2026-03.html" or None.
    """
    pattern = re.compile(r"^(\d{4})-(\d{2})\.html$")
    candidates: list[tuple[Path, tuple[int, int]]] = []
    for html_path in consolidated_dir.rglob("*.html"):
        try:
            rel = html_path.relative_to(consolidated_dir)
        except ValueError:
            continue
        parts = rel.parts
        if len(parts) == 2:  # YYYY/YYYY-MM.html
            try:
                y, m = int(parts[0]), int(parts[1].split("-")[1].split(".")[0])
                if pattern.match(parts[1]):
                    candidates.append((html_path, (y, m)))
            except (ValueError, IndexError):
                pass
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    _, (year, month) = candidates[0]
    return f"{year:04d}/{year:04d}-{month:02d}.html"


def get_consolidated_dir_from_output(output_path: Path) -> Path:
    """
    Get Consolidated directory from an output HTML path.
    Daily: Consolidated/YYYY/MM/file.html -> Consolidated
    Monthly: Consolidated/YYYY/file.html -> Consolidated
    """
    p = output_path.parent
    if len(p.name) == 2 and p.name.isdigit():  # MM directory (daily)
        return p.parent.parent
    return p.parent


def base_path_from_output(output_path: Path, consolidated_dir: Path) -> str:
    """
    Compute base_path (path from current page's directory to Consolidated/) from output_path.
    E.g. output_path=Consolidated/2026/03/2026-03-17.html -> "../../"
    """
    try:
        rel_dir = output_path.parent.relative_to(consolidated_dir)
    except ValueError:
        return ""
    depth = len(rel_dir.parts)
    return "../" * depth if depth else ""


def get_top_nav_html(base_path: str, monthly_report_path: str) -> str:
    """
    Generate the top menu bar HTML.

    Args:
        base_path: Path from current page's directory to Consolidated/ root.
                   E.g. "" for index.html, "../" for 2026/2026-03.html, "../../" for 2026/03/2026-03-17.html
        monthly_report_path: Path to latest monthly report relative to Consolidated/, e.g. "2026/2026-03.html"
    """
    base = base_path.rstrip("/")
    if base:
        base = base + "/"
    home_url = base + "index.html"
    monthly_url = base + monthly_report_path
    storage_url = base + "storage.html"
    users_url = base + "users/index.html"

    return f'''    <nav class="top-nav" aria-label="Main navigation">
        <a href="{html.escape(home_url)}" title="Home">
            {NAV_HOME_ICON}
            <span>Home</span>
        </a>
        <div class="top-nav-links">
            <a href="{html.escape(monthly_url)}" title="Monthly reports">
                {NAV_CALENDAR_ICON}
                <span>Monthly reports</span>
            </a>
            <a href="{html.escape(storage_url)}" title="Unattached volumes">
                {NAV_STORAGE_ICON}
                <span>Storage</span>
            </a>
            <a href="{html.escape(users_url)}" title="Per-user dashboards">
                {NAV_USERS_ICON}
                <span>Per-user dashboard</span>
            </a>
        </div>
    </nav>'''


def get_top_nav_css() -> str:
    """Return CSS for the top menu bar."""
    return """
        .top-nav { display: flex; align-items: center; justify-content: space-between; gap: 1rem; padding: 1rem 2rem; background: #0f172a; color: white; }
        .top-nav-links { display: flex; align-items: center; gap: 1rem; }
        .top-nav a { display: inline-flex; align-items: center; gap: 0.5rem; color: white; text-decoration: none; padding: 0.5rem 1rem; border-radius: 6px; font-weight: 500; transition: background 0.2s; }
        .top-nav a:hover { background: rgba(255,255,255,0.15); }
        .top-nav a svg { flex-shrink: 0; }
        @media (max-width: 768px) { .top-nav { flex-wrap: wrap; } }
"""


def get_filter_help_nav_icon_html() -> str:
    """(?) control placed immediately to the right of the reset-all-filters control."""
    return (
        '<span class="filter-help-btn nav-icon" title="How filters work" role="button" '
        'tabindex="0" aria-label="How filters work" aria-haspopup="dialog" '
        'aria-controls="filterHelpModal">?</span>'
    )


def get_filter_help_modal_html() -> str:
    """Dialog markup; include once per page (before </body>)."""
    return """    <div id="filterHelpModal" class="filter-help-modal" aria-hidden="true">
        <div class="filter-help-backdrop" tabindex="-1"></div>
        <div class="filter-help-dialog" role="dialog" aria-modal="true" aria-labelledby="filterHelpTitle">
            <h2 id="filterHelpTitle" class="filter-help-dialog-title">Charts &amp; filters</h2>
            <ul class="filter-help-list">
                <li><strong>Left-click</strong> a segment in a donut chart to set that chart&rsquo;s filter to the value you clicked. The table and other charts update to match.</li>
                <li><strong>Shift+click</strong> a segment to <em>exclude</em> that value instead (it appears struck through in the chart&rsquo;s dropdown). Shift+click again to clear the exclusion.</li>
            </ul>
            <p class="filter-help-oci"><strong>↗ Open in OCI Console</strong> (next to a resource name) opens that resource in the Oracle Cloud Console. It only works if you are already signed in to the <strong>same Oracle Cloud account (tenant)</strong> that owns that resource.</p>
            <button type="button" class="filter-help-close">Close</button>
        </div>
    </div>
"""


def get_filter_help_modal_css() -> str:
    """Styles for the (?) control, modal, and per-page nav alignment."""
    return """
        .filter-help-btn.nav-icon {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 2rem;
            height: 2rem;
            margin-left: 0.15rem;
            font-size: 0.95rem;
            font-weight: 700;
            line-height: 1;
            color: #0f172a;
            border-radius: 6px;
            background: #e2e8f0;
            cursor: pointer;
            user-select: none;
            box-sizing: border-box;
        }
        .filter-help-btn.nav-icon:hover { background: #cbd5e1; }
        .filter-help-modal {
            position: fixed;
            inset: 0;
            z-index: 10000;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 1rem;
            pointer-events: none;
            visibility: hidden;
            opacity: 0;
            transition: opacity 0.2s ease, visibility 0.2s ease;
        }
        .filter-help-modal.is-open {
            pointer-events: auto;
            visibility: visible;
            opacity: 1;
        }
        .filter-help-backdrop {
            position: absolute;
            inset: 0;
            background: rgba(15, 23, 42, 0.5);
        }
        .filter-help-dialog {
            position: relative;
            max-width: 32rem;
            width: 100%;
            max-height: min(90vh, 36rem);
            overflow-y: auto;
            background: #fff;
            color: #1e293b;
            border-radius: 10px;
            padding: 1.25rem 1.5rem 1.5rem;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.25);
        }
        .filter-help-dialog-title {
            margin: 0 0 0.75rem;
            font-size: 1.125rem;
            font-weight: 600;
            color: #0f172a;
        }
        .filter-help-list {
            margin: 0 0 1rem;
            padding-left: 1.25rem;
            line-height: 1.55;
            font-size: 0.9rem;
        }
        .filter-help-list li { margin-bottom: 0.6rem; }
        .filter-help-oci {
            margin: 0 0 1.25rem;
            font-size: 0.875rem;
            line-height: 1.5;
            color: #475569;
        }
        .filter-help-close {
            padding: 0.45rem 1rem;
            font-size: 0.875rem;
            font-weight: 500;
            color: #fff;
            background: #0f172a;
            border: none;
            border-radius: 6px;
            cursor: pointer;
        }
        .filter-help-close:hover { background: #1e293b; }
        body.filter-help-modal-open { overflow: hidden; }
"""


def get_filter_help_modal_init_js() -> str:
    """Attach open/close behavior; include after the main dashboard script in a script element."""
    return """
        (function initFilterHelpModal() {
            const modal = document.getElementById('filterHelpModal');
            if (!modal) return;
            const closeBtn = modal.querySelector('.filter-help-close');
            const backdrop = modal.querySelector('.filter-help-backdrop');
            function openModal() {
                modal.classList.add('is-open');
                modal.setAttribute('aria-hidden', 'false');
                document.body.classList.add('filter-help-modal-open');
                if (closeBtn) closeBtn.focus();
            }
            function closeModal() {
                modal.classList.remove('is-open');
                modal.setAttribute('aria-hidden', 'true');
                document.body.classList.remove('filter-help-modal-open');
            }
            document.querySelectorAll('.filter-help-btn').forEach(function (btn) {
                btn.addEventListener('click', function (e) { e.preventDefault(); openModal(); });
                btn.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openModal(); }
                });
            });
            if (closeBtn) closeBtn.addEventListener('click', closeModal);
            if (backdrop) backdrop.addEventListener('click', closeModal);
            document.addEventListener('keydown', function (e) {
                if (e.key === 'Escape' && modal.classList.contains('is-open')) closeModal();
            });
        })();
"""
