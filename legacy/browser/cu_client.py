"""
Generic Claude Computer Use loop over Playwright (Tasman pattern).

Instead of brittle CSS selectors, Claude looks at a screenshot of the actual
screen and decides what action to take next. Used ONLY for perception and
navigation in read-only portal areas; actions with financial effect are
forbidden by the system prompt and are never part of any goal.

Loop protocol (required by the API):
  user   → [goal text + screenshot image]
  asst   → [tool_use blocks]
  user   → [tool_result blocks (one per tool_use) + new screenshot]
  asst   → [more tool_use OR plain text when done]
"""

import base64

import anthropic

from agent.prompts import cu_system_prompt
from config.settings import CU_MODEL
from utils.token_tracker import tracker

CU_TOOLS = [
    {
        "type": "computer_20250124",
        "name": "computer",
        "display_width_px": 1280,
        "display_height_px": 800,
    }
]


async def _screenshot_b64(page) -> str:
    data = await page.screenshot(type="png")
    return base64.standard_b64encode(data).decode("utf-8")


async def _execute_action(page, action: dict):
    """Execute a single computer-use action dict from Claude via Playwright."""
    atype = action.get("action") or action.get("type")

    if atype == "screenshot":
        return  # fresh screenshot is attached in the loop

    if atype in ("left_click", "click"):
        x, y = action["coordinate"]
        await page.mouse.click(x, y)
        await page.wait_for_timeout(600)

    elif atype == "double_click":
        x, y = action["coordinate"]
        await page.mouse.dblclick(x, y)
        await page.wait_for_timeout(600)

    elif atype == "mouse_move":
        x, y = action["coordinate"]
        await page.mouse.move(x, y)

    elif atype == "type":
        await page.keyboard.type(action["text"], delay=40)
        await page.wait_for_timeout(300)

    elif atype == "key":
        keys = action["text"]
        key_map = {"Return": "Enter", "ctrl+a": "Control+a", "ctrl+c": "Control+c"}
        await page.keyboard.press(key_map.get(keys, keys))
        await page.wait_for_timeout(300)

    elif atype == "scroll":
        x, y = action["coordinate"]
        direction = action.get("scroll_direction", "down")
        amount = action.get("scroll_distance", 3)
        dy = -amount * 100 if direction == "up" else amount * 100
        await page.mouse.wheel(0, dy)
        await page.wait_for_timeout(400)

    elif atype == "wait":
        await page.wait_for_timeout(action.get("ms", 1000))


async def run_cu_loop(
    page,
    client: anthropic.Anthropic,
    goal: str,
    portal_name: str,
    allowed_scope: str,
    max_steps: int = 20,
) -> str:
    """
    Run the computer-use loop until Claude answers with plain text.
    Returns Claude's final text (portal drivers parse keywords out of it).
    """
    img_b64 = await _screenshot_b64(page)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": goal},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
                },
            ],
        }
    ]

    system = cu_system_prompt(portal_name, allowed_scope)

    for step in range(max_steps):
        response = client.beta.messages.create(
            model=CU_MODEL,
            max_tokens=1024,
            tools=CU_TOOLS,
            messages=messages,
            betas=["computer-use-2025-01-24"],
            system=system,
        )
        tracker.track(
            CU_MODEL,
            response.usage.input_tokens,
            response.usage.output_tokens,
            call_type="computer-use",
        )

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        for block in assistant_content:
            if hasattr(block, "text") and block.text.strip():
                print(f"   💭 {block.text.strip()}")

        if response.stop_reason == "end_turn":
            for block in assistant_content:
                if hasattr(block, "text"):
                    final = block.text.strip()
                    print(f"   ✅ Claude: {final}")
                    return final
            return "done"

        tool_results = []
        acted = False
        for block in assistant_content:
            if block.type == "tool_use" and block.name == "computer":
                action = block.input
                label = action.get("action") or action.get("type", "?")
                detail = action.get("coordinate", action.get("text", ""))
                print(f"   🖱️  [{step + 1}] {label} {detail}")
                await _execute_action(page, action)
                acted = True

                new_img_b64 = await _screenshot_b64(page)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": new_img_b64,
                                },
                            }
                        ],
                    }
                )

        if not acted:
            break

        messages.append({"role": "user", "content": tool_results})

    return "loop_complete"


async def read_screen(page, client: anthropic.Anthropic, extraction_prompt: str) -> str:
    """
    Single-shot vision read (no actions): screenshot + extraction prompt.
    Used to read balances, table headers or export dialogs visually.
    Returns raw model text; caller parses.
    """
    img_b64 = await _screenshot_b64(page)
    response = client.messages.create(
        model=CU_MODEL,
        max_tokens=2048,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": extraction_prompt},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": img_b64},
                    },
                ],
            }
        ],
    )
    tracker.track(
        CU_MODEL,
        response.usage.input_tokens,
        response.usage.output_tokens,
        call_type="vision-read",
    )
    return response.content[0].text.strip()
