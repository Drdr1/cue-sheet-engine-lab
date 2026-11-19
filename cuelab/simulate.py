"""Synthetic radio shows for testing: music, talk breaks and station IDs."""
from __future__ import annotations

import random

ARTISTS = ["The Northern Lights", "Delta Rail", "Kenai Blue", "Tanana Drift", "Copper River Band", "Midnight Sun Trio"]
WORDS = ["Aurora", "Frozen", "Highway", "Cabin", "Spruce", "Tundra", "Moose", "Lantern", "Glacier", "Embers"]
LABELS = ["Borealis Records", "Yukon Sound", "Coldwater Music"]


def make_show(duration_s: float, seed: int = 1, *, talk_every: int = 4) -> list[dict]:
    """Songs of 2.5-5 minutes, a talk break every few songs, a station ID each hour."""
    rng = random.Random(seed)
    items, t, n = [], 0.0, 0
    next_id = 0.0
    while t < duration_s:
        if t >= next_id:
            end = min(t + 15, duration_s)
            items.append({"start": t, "end": end, "kind": "id", "title": "Station ID"})
            t, next_id = end, next_id + 3600
            continue
        n += 1
        if n % (talk_every + 1) == 0:
            end = min(t + rng.uniform(60, 180), duration_s)
            items.append({"start": t, "end": end, "kind": "talk", "title": "Talk"})
        else:
            end = min(t + rng.uniform(150, 300), duration_s)
            title = f"{rng.choice(WORDS)} {rng.choice(WORDS)}"
            items.append(
                {
                    "start": t,
                    "end": end,
                    "kind": "music",
                    "title": title,
                    "artist": rng.choice(ARTISTS),
                    "album": f"{rng.choice(WORDS)} Sessions",
                    "label": rng.choice(LABELS),
                    "isrc": f"US-X{rng.randint(10, 99)}-{rng.randint(20, 26)}-{rng.randint(10000, 99999)}",
                }
            )
        t = end
    return items
