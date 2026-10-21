"""Computer use: Claude reads web pages the way a person does.

`browser.Browser` is the screen and the hands (a headless Chromium driven by
Playwright); `agent.run` is the loop that shows Claude a screenshot, performs
the action it asks for and repeats until it answers; `tradingview` uses both
to read spot prices from TradingView symbol pages.
"""
