"""One-time/repeatable migration for stable saved-search IDs and categories."""

import json
import re
from pathlib import Path

import ebay_deal_alert as alert
import platforms


CONFIG_PATH = Path(__file__).resolve().with_name("config.json")
GOLF_EQUIPMENT_TERMS = (
    "golf", " iron", "irons", "driver", "putter", "wedge", "stand bag",
    "taylormade", "callaway", "ping g", "cobra f", "cleveland rtx",
    "odyssey", "titleist ap", "mizuno jp", "nike vapor",
)


def _clean_query(query):
    return platforms.split_query_exclusions(query)[0].strip().strip('"')


def _category_for(search):
    clean = _clean_query(search.get("query", ""))
    platforms_for_search = set(search.get("platforms") or [])
    if {"shopgoodwill", "facebook"}.issubset(platforms_for_search) and any(
        term in f" {clean.lower()}" for term in GOLF_EQUIPMENT_TERMS
    ):
        return "golf-equipment"
    return alert.classify_search_category(clean)


def _base_id(category, query):
    clean = _clean_query(query).lower()
    words = re.findall(r"[a-z0-9]+", clean)
    category_prefix = {
        "watches": "watch",
        "golf-equipment": "golf",
        "school-gear": "school",
        "poker-chips": "poker",
    }.get(category, category)
    redundant = {
        "watches": {"watch", "watches"},
        "golf-equipment": {"golf", "club", "clubs", "set"},
        "school-gear": {"gamecocks"},
        "poker-chips": {"poker", "casino", "chip", "chips"},
    }.get(category, set())
    detail = [word for word in words if word not in redundant]
    slug = "-".join([category_prefix, *(detail or words)])
    return slug[:56].strip("-")


def migrate(config):
    used = set()
    for search in config["SAVED_SEARCHES"]:
        category = _category_for(search)
        base = _base_id(category, search.get("query", ""))
        candidate = base
        suffix = 2
        while candidate in used:
            suffix_text = f"-{suffix}"
            candidate = base[: 64 - len(suffix_text)].rstrip("-") + suffix_text
            suffix += 1
        used.add(candidate)
        search["id"] = candidate
        search["category"] = category
    return config


if __name__ == "__main__":
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    migrated = migrate(config)
    CONFIG_PATH.write_text(
        json.dumps(migrated, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="",
    )
    print(f"Migrated {len(migrated['SAVED_SEARCHES'])} saved searches")
