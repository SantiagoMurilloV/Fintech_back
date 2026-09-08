"""
Connects Playwright to an already-running Chrome via CDP (Tasman pattern).

The human launches Chrome with remote debugging, logs into the bank /
accounting portal (2FA included), then runs the agent. The agent attaches
to the existing session — no credentials ever touch the codebase.

Start Chrome with debugging enabled (once, in a terminal):
  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222
"""

CDP_URL = "http://127.0.0.1:9222"


async def get_browser_context(playwright):
    print(f"🔌 Connecting to your running Chrome on {CDP_URL} ...")
    browser = await playwright.chromium.connect_over_cdp(CDP_URL)

    # Use the first existing context (the user's real Chrome session)
    contexts = browser.contexts
    if contexts:
        return contexts[0]

    # Fallback: create a new context in the existing browser
    return await browser.new_context()


async def get_page(context, url_hint: str = ""):
    """
    Return the tab whose URL contains url_hint, or the first open tab,
    or a fresh one if the browser has none.
    """
    pages = context.pages
    if url_hint:
        for page in pages:
            if url_hint in page.url:
                return page
    if pages:
        return pages[0]
    return await context.new_page()
