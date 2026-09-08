"""
Generic bank portal driver, configured from portals.yaml.

Any bank/payment portal can be onboarded without writing code: add an entry
to portals.yaml with its name, module deep-link and a description of the
movements screen. The first (assisted) run teaches the skills; later runs
get progressively more deterministic.
"""

from browser.portals.base_portal import BasePortal


class GenericBankPortal(BasePortal):
    def __init__(self, page, client, config: dict):
        super().__init__(page, client)
        self.name = config["name"]
        self.module_url = config.get("module_url", "")
        self.module_description = config.get(
            "module_description", "account movements list with a date filter"
        )
        self.allowed_scope = config.get(
            "allowed_scope", "account movements / statements module"
        )
        self._date_selectors = config.get("date_selectors", {})  # optional learned CSS

    async def _set_period_deterministic(self, selector_note: str, date_from: str, date_to: str):
        """
        Deterministic date-range fill using selectors from portals.yaml or a
        learned skill note (format: `selector: <from_css> | <to_css> | <apply_css>`).
        """
        parts = [s.strip() for s in selector_note.split("|")]
        if len(parts) != 3:
            raise ValueError(f"Bad selector note: {selector_note!r}")
        from_css, to_css, apply_css = parts

        await self.page.fill(from_css, date_from)
        await self.page.fill(to_css, date_to)
        await self.page.click(apply_css)
        await self.page.wait_for_timeout(2000)
        print(f"✅ [{self.name}] Period {date_from}→{date_to} set deterministically")
