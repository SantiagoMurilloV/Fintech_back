"""The screen and the hands for computer use: a headless Chromium.

Claude never touches the machine: it asks for an action ("left_click at
640, 300", "type ...", "screenshot") and this class performs it on a
browser page and returns a fresh screenshot. The page is the whole screen,
so coordinates map one to one onto the viewport.

Playwright is imported lazily: a server without the browser installed still
starts, and the module reports «browser not installed» instead of crashing.
"""
from __future__ import annotations

import base64
import time

WIDTH, HEIGHT = 1280, 800

# Claude's key names -> Playwright's.
KEYS = {
    "return": "Enter", "enter": "Enter", "esc": "Escape", "escape": "Escape",
    "tab": "Tab", "backspace": "Backspace", "delete": "Delete", "space": " ",
    "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    "page_down": "PageDown", "page_up": "PageUp", "home": "Home", "end": "End",
    "ctrl": "Control", "cmd": "Meta", "super": "Meta", "alt": "Alt", "shift": "Shift",
}


# Runs in the page: candidates for the element showing a number.
DERIVE_JS = r"""
(value) => {
  const norm = (t) => parseFloat(String(t).replace(/[^0-9.\-]/g, ''));
  const close = (a) => Math.abs(a - value) <= Math.max(Math.abs(value) * 1e-6, 1e-9);
  const hashed = (c) => /[-_][A-Za-z0-9]{6,}$/.test(c) && /[A-Z]|\d/.test(c.slice(-6));
  const options = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length) continue;
    const text = (el.textContent || '').trim();
    if (!text || text.length > 20 || !close(norm(text))) continue;
    const size = parseFloat(getComputedStyle(el).fontSize) || 0;
    const tag = el.tagName.toLowerCase();
    const picks = [];
    for (const attr of ['data-qa-id', 'data-test', 'data-testid', 'data-field']) {
      const v = el.getAttribute(attr);
      if (v) picks.push(`${tag}[${attr}="${v}"]`);
    }
    if (el.id && !hashed(el.id)) picks.push(`#${CSS.escape(el.id)}`);
    for (const c of el.classList) if (!hashed(c)) picks.push(`${tag}.${CSS.escape(c)}`);
    for (const sel of picks) {
      const first = document.querySelector(sel);
      if (first === el) options.push({sel, size});
    }
  }
  options.sort((a, b) => b.size - a.size);
  return options.length ? options[0].sel : null;
}
"""


class BrowserUnavailable(RuntimeError):
    pass


def _key(combo: str) -> str:
    parts = [KEYS.get(part.strip().lower(), part.strip()) for part in combo.split("+")]
    return "+".join(parts)


class Browser:
    """One headless page; use as a context manager."""

    def __init__(self, width: int = WIDTH, height: int = HEIGHT):
        self.width, self.height = width, height
        self._pw = self._browser = self.page = None

    def __enter__(self) -> Browser:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserUnavailable(
                "El navegador para computer use no está instalado (playwright).") from None
        try:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        except Exception as err:  # noqa: BLE001 — chromium binary missing, sandbox, etc.
            if self._pw:
                self._pw.stop()
            raise BrowserUnavailable(f"No pude abrir Chromium: {err}") from None
        context = self._browser.new_context(
            viewport={"width": self.width, "height": self.height}, locale="en-US",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"))
        self.page = context.new_page()
        return self

    def __exit__(self, *_exc) -> None:
        for closer in (self._browser, self._pw):
            try:
                if closer is self._pw and closer:
                    closer.stop()
                elif closer:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ screen

    def goto(self, url: str, settle_ms: int = 2500) -> None:
        """Open a page in code (cheaper than asking Claude to type the URL)."""
        self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        self.page.wait_for_timeout(settle_ms)

    @property
    def url(self) -> str:
        return self.page.url

    def read_text(self, selector: str, timeout_ms: int = 8000) -> str:
        """The text of the first element matching `selector` (the recipe step)."""
        locator = self.page.locator(selector).first
        locator.wait_for(state="visible", timeout=timeout_ms)
        return (locator.inner_text(timeout=timeout_ms) or "").strip()

    def derive_selector(self, value: float) -> str | None:
        """A stable CSS selector for the element showing `value`, or None.

        Used after computer use read a price: find the element whose text is
        that number, prefer attributes made to be stable (data-qa-id, id,
        js-* classes) over generated class names, and keep only a selector
        whose first match really shows the value. The largest such element
        wins — the headline price, not a figure in a side table.
        """
        return self.page.evaluate(DERIVE_JS, value)

    def screenshot(self) -> str:
        """The screen as base64 PNG, the format the tool result carries."""
        return base64.b64encode(self.page.screenshot(type="png")).decode()

    # ------------------------------------------------------------------ hands

    def act(self, action: dict) -> str | None:
        """Perform one computer-use action; returns a screenshot (base64) or None.

        Unknown actions raise ValueError so the agent can tell Claude.
        """
        kind = action.get("action")
        page = self.page
        x, y = (action.get("coordinate") or [None, None])[:2]
        if kind == "screenshot":
            pass
        elif kind in ("left_click", "right_click", "middle_click", "double_click",
                      "triple_click"):
            button = {"right_click": "right", "middle_click": "middle"}.get(kind, "left")
            clicks = {"double_click": 2, "triple_click": 3}.get(kind, 1)
            page.mouse.click(x, y, button=button, click_count=clicks)
        elif kind == "mouse_move":
            page.mouse.move(x, y)
        elif kind == "left_click_drag":
            start = action.get("start_coordinate") or [x, y]
            page.mouse.move(*start)
            page.mouse.down()
            page.mouse.move(x, y)
            page.mouse.up()
        elif kind == "type":
            page.keyboard.type(str(action.get("text", "")), delay=15)
        elif kind == "key":
            page.keyboard.press(_key(str(action.get("text", ""))))
        elif kind == "scroll":
            amount = int(action.get("scroll_amount") or 3) * 100
            direction = action.get("scroll_direction", "down")
            dx = {"left": -amount, "right": amount}.get(direction, 0)
            dy = {"up": -amount, "down": amount}.get(direction, 0)
            if x is not None:
                page.mouse.move(x, y)
            page.mouse.wheel(dx, dy)
        elif kind == "wait":
            time.sleep(min(float(action.get("duration") or 1), 5))
        else:
            raise ValueError(f"Acción no soportada: {kind}")
        page.wait_for_timeout(600)
        return self.screenshot()
