"""
Hybrid portal driver base class — the heart of the Tasman pattern.

Division of labor (learned from Tasman's send_message incident):
  - Computer Use  → perception: assess state, find UI elements, handle unknown layouts
  - Playwright    → critical actions: known selectors, date inputs, download capture
  - Skills (.md)  → memory: what worked last run (selectors, coordinates, quirks)

A portal starts fully CU-driven; as skills accumulate learned selectors and
coordinates, runs become faster, cheaper and deterministic.
"""

from pathlib import Path

import anthropic

from agent.prompts import ASSESS_STATE_GOAL
from browser.cu_client import run_cu_loop
from config.settings import DOWNLOADS_DIR
from skills.skills_manager import load_skill, update_skill


class BasePortal:
    """Subclass per portal (or use GenericBankPortal driven by portals.yaml)."""

    name = "base"
    module_url = ""            # deep link to the read-only module, if known
    module_description = ""    # e.g. "account movements list with a date filter"
    allowed_scope = ""         # e.g. "account movements / statements module"

    def __init__(self, page, client: anthropic.Anthropic):
        self.page = page
        self.client = client

    # ── State assessment (CU, perception only) ──────────────────────────────

    async def ensure_at_module(self) -> bool:
        """
        Tasman entry-point pattern: CU looks at the screen and classifies state.
        - at_module         → nothing to do
        - needs_navigation  → deterministic page.goto to the deep link
        - needs_login       → hand control to the human (2FA etc.), then retry
        """
        goal = ASSESS_STATE_GOAL.format(
            module_description=self.module_description, portal_name=self.name
        )
        result = await run_cu_loop(
            self.page, self.client, goal, self.name, self.allowed_scope, max_steps=3
        )

        if "at_module" in result:
            print(f"✅ [{self.name}] Already at module")
            return True

        if "needs_navigation" in result and self.module_url:
            print(f"🔀 [{self.name}] Navigating to module...")
            await self.page.goto(self.module_url, wait_until="domcontentloaded", timeout=30000)
            await self.page.wait_for_timeout(2000)
            return True

        # needs_login (or unknown) — human takes over, agent never touches auth
        print("\n" + "=" * 60)
        print(f"🔐 [{self.name}] Please log in manually in the Chrome window.")
        print("   Complete any 2FA/OTP steps. The agent never handles credentials.")
        print("=" * 60)
        input("\n▶  Press Enter when you're logged in: ")
        if self.module_url:
            await self.page.goto(self.module_url, wait_until="domcontentloaded", timeout=30000)
            await self.page.wait_for_timeout(2000)
        return True

    # ── Period filter (Playwright-first, CU fallback) ────────────────────────

    async def set_period(self, date_from: str, date_to: str) -> bool:
        """Try the learned selector first; fall back to a CU-guided attempt."""
        skill = load_skill(self.name, "set_date_range")

        selector = _extract_learned_selector(skill)
        if selector:
            try:
                await self._set_period_deterministic(selector, date_from, date_to)
                return True
            except Exception as e:
                print(f"⚠️  [{self.name}] Learned selector failed ({e}) — falling back to CU")

        goal = f"""
You are looking at {self.name}, at the {self.module_description}.

Your ONLY goal: set the date-range filter to {date_from} → {date_to}.

--- SKILL: set_date_range (notes from previous runs) ---
{skill}

Steps:
1. Locate the date filter using the skill notes above.
2. Set the FROM date to {date_from} and the TO date to {date_to}.
3. Apply the filter (button like 'Consultar', 'Buscar', 'Aplicar', 'Filtrar').
4. Confirm with "period_set: x,y" (coordinates of the filter you used),
   or "no_date_filter" if none exists on this screen.
"""
        result = await run_cu_loop(
            self.page, self.client, goal, self.name, self.allowed_scope, max_steps=15
        )
        if "period_set" in result:
            update_skill(self.name, "set_date_range",
                         f"CU set period {date_from}→{date_to} successfully",
                         _parse_coords(result, "period_set"))
            return True
        return False

    async def _set_period_deterministic(self, selector_note: str, date_from: str, date_to: str):
        """Override per portal once real selectors are learned. Raises by default."""
        raise NotImplementedError("No deterministic date-range routine for this portal yet")

    # ── Export / download (CU finds the button, Playwright captures the file) ─

    async def export_statement(self, period_label: str) -> Path | None:
        """
        CU locates and clicks the export control; Playwright's expect_download
        deterministically captures the file (Tasman rule: critical action = PW).
        """
        skill = load_skill(self.name, "find_export_button")

        goal = f"""
You are looking at {self.name}, at the {self.module_description},
already filtered to the desired period.

Your ONLY goal: trigger the export/download of the movements list
(prefer Excel/CSV over PDF if a format choice appears).

--- SKILL: find_export_button (notes from previous runs) ---
{skill}

Steps:
1. Locate the export control (icons/labels like 'Exportar', 'Descargar', ⬇, CSV, XLS).
2. Click it. If a format dialog appears, choose Excel or CSV.
3. Confirm with "export_clicked: x,y", or "no_export_button" if none exists.
"""
        result = ""
        try:
            async with self.page.expect_download(timeout=60000) as download_info:
                result = await run_cu_loop(
                    self.page, self.client, goal, self.name, self.allowed_scope, max_steps=12
                )
                if "no_export_button" in result:
                    print(f"❌ [{self.name}] No export button found")
                    return None
            download = await download_info.value
        except Exception as e:
            print(f"❌ [{self.name}] Download not captured: {e}")
            return None

        suffix = Path(download.suggested_filename).suffix or ".xlsx"
        dest = DOWNLOADS_DIR / f"{self.name}_{period_label}{suffix}"
        await download.save_as(dest)
        update_skill(self.name, "find_export_button",
                     f"Export captured as {dest.name}", _parse_coords(result, "export_clicked"))
        print(f"📥 [{self.name}] Statement saved: {dest}")
        return dest

    # ── Full collection routine ──────────────────────────────────────────────

    async def collect(self, date_from: str, date_to: str, period_label: str) -> Path | None:
        await self.ensure_at_module()
        await self.set_period(date_from, date_to)
        return await self.export_statement(period_label)


def _extract_learned_selector(skill_text: str) -> str | None:
    """Skills may record a working CSS selector as: `selector: <css>`."""
    for line in skill_text.splitlines():
        if line.strip().lower().startswith("selector:"):
            return line.split(":", 1)[1].strip()
    return None


def _parse_coords(result: str, keyword: str) -> list | None:
    import re

    m = re.search(rf"{keyword}:\s*(\d+),\s*(\d+)", result)
    return [int(m.group(1)), int(m.group(2))] if m else None
