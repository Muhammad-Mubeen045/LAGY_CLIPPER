"""Title / description / hashtag generation for each clip.

Titles stay under 100 characters (YouTube Shorts best practice).
Every clip carries a source credit — the original channel and stream URL.
"""
from __future__ import annotations

import re

HASHTAGS = ["#GTA6", "#GTA6Clips", "#ViceCity", "#Leonida", "#Shorts", "#Gaming"]

TITLE_TEMPLATES = [
    "GTA 6 {hook} 😱 | {channel} Live Moment",
    "GTA 6 {hook} 🔥 #Shorts",
    "{channel} GTA 6 Live — {hook}!",
    "GTA 6 {hook} — You Have To See This 👀",
    "Best GTA 6 Live Moment: {hook} 🎮",
]

HOOKS = {
    "clutch": "CLUTCH Play",
    "insane": "INSANE Moment",
    "wow": "WOW Moment",
    "funny": "Funniest Moment",
    "kill": "Crazy Kill",
    "win": "Big Win",
    "goal": "Unbelievable Play",
    "gta": "GTA 6 Moment",
    "vice city": "Vice City Moment",
    "lucia": "Lucia Goes Wild",
    "jason": "Jason Goes Wild",
    "leonida": "Leonida Moment",
}


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text


def _hook_for(keywords: list[str], rank: int) -> str:
    for kw in keywords:
        if kw in HOOKS:
            return HOOKS[kw]
    return ["Top Moment", "Best Moment", "Wild Moment", "Craziest Moment"][(rank - 1) % 4]


def make_package(meta: dict, moment: dict, index: int) -> dict:
    """Return {title, description, hashtags, credit} for one clip."""
    channel = meta.get("channel", "Unknown channel")
    source_title = _clean(meta.get("title", "GTA 6 Live"))
    hook = _hook_for(moment.get("keywords") or [], moment.get("rank", index))
    template = TITLE_TEMPLATES[(index - 1) % len(TITLE_TEMPLATES)]
    title = template.format(hook=hook, channel=channel)
    if len(title) > 100:
        title = title[:97] + "..."
    ts = moment.get("timestamp", "")
    url = meta.get("webpage_url", "")
    description = (
        f"{hook} from the GTA 6 live stream by {channel} 🎮\n"
        f"Original moment at {ts} in: {source_title}\n\n"
        f"Source: {channel} — {url}\n"
        f"Credit: original stream belongs to {channel}. "
        f"This clip is a short highlight for commentary / reaction.\n\n"
        + " ".join(HASHTAGS)
    )
    return {
        "title": title,
        "description": description,
        "hashtags": HASHTAGS,
        "credit": f"Source: {channel} — {url}",
        "source_title": source_title,
        "hook": hook,
    }
