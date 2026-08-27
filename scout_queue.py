"""Read and normalize browser-discovered Scout listings.

The queue is deliberately a transport boundary only.  Scoring, dedupe and
alerts remain in ebay_deal_alert.py's existing pipeline.
"""

import json
import logging
from pathlib import Path

from platforms import make_listing


logger = logging.getLogger("scout_queue")
SCOUT_QUEUE_PATH = Path(__file__).resolve().with_name("scout_queue.jsonl")


def load_scout_queue(path=None):
    """Return normalized listings; malformed/unusable rows never raise."""
    queue_path = Path(path) if path is not None else SCOUT_QUEUE_PATH
    try:
        text = queue_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Could not read Scout queue %s: %s", queue_path, exc)
        return []

    listings = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
            if not isinstance(row, dict):
                raise ValueError("row is not an object")
            platform = row.get("platform")
            item_id = row.get("itemId")
            if not isinstance(platform, str) or not platform.strip():
                raise ValueError("platform is not a non-empty string")
            for field in ("itemId", "title", "itemWebUrl"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise ValueError(f"{field} is not a non-empty string")
            if row.get("price") is None or isinstance(row.get("price"), bool) or not isinstance(row.get("price"), (int, float)):
                raise ValueError("price is not a number")
            for field in ("imageUrl", "description"):
                if field in row and row[field] is not None and not isinstance(row[field], str):
                    raise ValueError(f"{field} is not a string")
            if isinstance(item_id, str) and isinstance(platform, str):
                prefix = f"{platform}:"
                if item_id.startswith(prefix):
                    item_id = item_id[len(prefix):]
            listing = make_listing(
                platform,
                item_id,
                row.get("title"),
                row.get("price"),
                row.get("itemWebUrl"),
                image_url=row.get("imageUrl"),
                description=row.get("description"),
            )
            if listing is None:
                raise ValueError("missing a required platform/itemId/title/price/itemWebUrl value")
            listings.append(listing)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Skipping malformed Scout queue line %s: %s", line_number, exc)
    return listings


def scout_queue_has_data(path=None):
    """Whether a run has queue content to consume, including malformed rows."""
    queue_path = Path(path) if path is not None else SCOUT_QUEUE_PATH
    try:
        return bool(queue_path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError):
        return False


def clear_scout_queue(path=None):
    """Empty the consumed queue. Missing files and I/O failures are harmless."""
    queue_path = Path(path) if path is not None else SCOUT_QUEUE_PATH
    try:
        queue_path.write_text("", encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("Could not clear Scout queue %s: %s", queue_path, exc)
        return False
