"""Graph analytics: orphans, dead links, clusters, duplicates."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any


def analyze_vault(vault: Any, memory: Any | None = None) -> dict[str, Any]:
    notes = []
    for meta in vault.list_notes():
        full = vault.read_note(meta["id"])
        if full:
            notes.append(full)

    by_id = {n["id"]: n for n in notes}
    titles = {n["id"]: (n.get("title") or n["id"]).lower() for n in notes}
    title_to_ids: dict[str, list[str]] = defaultdict(list)
    for n in notes:
        title_to_ids[(n.get("title") or n["id"]).lower().strip()].append(n["id"])

    # link graph
    out_links: dict[str, list[str]] = defaultdict(list)
    in_count: dict[str, int] = defaultdict(int)
    dead: list[dict[str, str]] = []
    for n in notes:
        for link in n.get("links") or []:
            lid = None
            # match by stem or title
            for nid, t in titles.items():
                if nid.lower() == link.lower() or t == link.lower():
                    lid = nid
                    break
            if lid:
                out_links[n["id"]].append(lid)
                in_count[lid] += 1
            else:
                dead.append({"from": n["id"], "from_title": n.get("title") or n["id"], "to": link})

    orphans = []
    for n in notes:
        if not out_links.get(n["id"]) and not in_count.get(n["id"]):
            orphans.append({"id": n["id"], "title": n.get("title") or n["id"]})

    # near-duplicate titles (simple)
    duplicates = []
    for t, ids in title_to_ids.items():
        if len(ids) > 1:
            duplicates.append({"title": t, "ids": ids})
    # fuzzy: share long prefix
    ids_list = list(by_id.keys())
    for i, a in enumerate(ids_list):
        ta = titles[a]
        for b in ids_list[i + 1 :]:
            tb = titles[b]
            if ta == tb:
                continue
            if ta in tb or tb in ta:
                if min(len(ta), len(tb)) >= 6:
                    duplicates.append({"title": f"{ta} ~ {tb}", "ids": [a, b]})

    # cluster density
    clusters: dict[str, int] = defaultdict(int)
    for n in notes:
        tags = " ".join(n.get("tags") or []).lower()
        typ = (n.get("type") or "").lower()
        hay = tags + " " + typ + " " + (n.get("title") or "").lower()
        if re.search(r"speech|voice|talk|language", hay):
            clusters["speech"] += 1
        elif re.search(r"creative|art|design|story", hay):
            clusters["creative"] += 1
        elif re.search(r"person|session|interact|chat", hay):
            clusters["interactive"] += 1
        else:
            clusters["analytical"] += 1

    usage = {}
    if memory is not None:
        usage = {
            "touched": memory.stats().get("touched_notes"),
            "total_hits": memory.stats().get("total_hits"),
        }

    # weakest: low links + low strength
    weakest = []
    for n in notes:
        strength = 0.0
        if memory is not None:
            u = memory.usage.get(n["id"]) or {}
            strength = float(u.get("strength") or 0)
        score = strength + len(n.get("links") or []) + (n.get("word_count") or 0) / 100
        weakest.append(
            {
                "id": n["id"],
                "title": n.get("title") or n["id"],
                "score": round(score, 2),
                "links": len(n.get("links") or []),
            }
        )
    weakest.sort(key=lambda x: x["score"])

    return {
        "notes": len(notes),
        "orphans": orphans[:40],
        "orphan_count": len(orphans),
        "dead_links": dead[:40],
        "dead_link_count": len(dead),
        "duplicates": duplicates[:30],
        "clusters": dict(clusters),
        "weakest": weakest[:15],
        "usage": usage,
        "avg_links": round(
            sum(len(n.get("links") or []) for n in notes) / max(1, len(notes)), 2
        ),
    }
