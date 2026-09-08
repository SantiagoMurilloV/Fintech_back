"""
Skills manager (Tasman pattern, extended with per-portal subfolders).

Each portal has a folder of skill .md files loaded as context for the
computer-use loop and updated after successful actions, so the agent
learns the portal's UI run after run.
"""

from datetime import datetime
from pathlib import Path

SKILLS_DIR = Path(__file__).parent

SKILL_TEMPLATE = """# Skill: {skill_name} — {portal}

## Purpose
{purpose}

## Last known location
_Unknown — will be updated after first successful run._

## Notes from previous runs
_No runs recorded yet._
"""


def _skill_path(portal: str, skill_name: str) -> Path:
    return SKILLS_DIR / portal / f"{skill_name}.md"


def load_skill(portal: str, skill_name: str) -> str:
    """Read a portal skill file; create a blank template if missing."""
    path = _skill_path(portal, skill_name)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            SKILL_TEMPLATE.format(
                skill_name=skill_name,
                portal=portal,
                purpose=f"Notes to help locate and use '{skill_name}' in {portal}.",
            )
        )
    return path.read_text()


def update_skill(portal: str, skill_name: str, observation: str, coords: list | None = None):
    """Append an observation after a successful action — how the agent learns."""
    path = _skill_path(portal, skill_name)
    if not path.exists():
        load_skill(portal, skill_name)

    content = path.read_text()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    note = f"\n- [{timestamp}] {observation}"
    if coords:
        note += f" (approx. coordinates: {coords})"

    content = content.replace("_No runs recorded yet._", "")
    content = content.replace(
        "## Notes from previous runs", f"## Notes from previous runs{note}"
    )

    if coords and "## Last known location" in content:
        import re

        location = f"x={coords[0]}, y={coords[1]} (last seen {timestamp})"
        content = content.replace(
            "_Unknown — will be updated after first successful run._", location
        )
        content = re.sub(r"x=\d+, y=\d+ \(last seen .*?\)", location, content)

    path.write_text(content)
