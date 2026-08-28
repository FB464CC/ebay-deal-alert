"""Read and normalize browser-discovered Scout listings.

The queue is deliberately a transport boundary only.  Scoring, dedupe and
alerts remain in ebay_deal_alert.py's existing pipeline.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

from platforms import make_listing


logger = logging.getLogger("scout_queue")
SCOUT_QUEUE_PATH = Path(__file__).resolve().with_name("scout_queue.jsonl")


def scout_listing_key(listing):
    """Return the stable transport key used to acknowledge a queue row."""
    platform = listing.get("platform")
    item_id = listing.get("itemId")
    if not isinstance(platform, str) or not isinstance(item_id, str):
        return None
    platform = platform.strip().lower()
    item_id = item_id.strip()
    prefix = f"{platform}:"
    if item_id.lower().startswith(prefix):
        item_id = item_id[len(prefix):]
    if not platform or not item_id:
        return None
    return f"{platform}:{item_id}"


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
            for field in ("scoutSearchQuery", "scoutSearchLabel"):
                if field in row and row[field] is not None and (
                    not isinstance(row[field], str) or not row[field].strip()
                ):
                    raise ValueError(f"{field} is not a non-empty string")
            queue_key = scout_listing_key(row)
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
            # Internal transport metadata. It survives the merge into the
            # scoring pipeline but is never sent to users or providers.
            listing["_scout_queue_key"] = queue_key
            if row.get("scoutSearchQuery"):
                listing["_scout_search_query"] = row["scoutSearchQuery"].strip()
            if row.get("scoutSearchLabel"):
                listing["_scout_search_label"] = row["scoutSearchLabel"].strip()
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


def remove_processed_scout_queue(processed_keys, path=None):
    """Atomically remove only queue rows acknowledged by the poller.

    Malformed rows and valid rows whose keys are absent are copied verbatim
    into the replacement file. On any read/write error the original queue is
    left in place, making the operation fail toward a harmless retry.
    """
    queue_path = Path(path) if path is not None else SCOUT_QUEUE_PATH
    processed = {key for key in processed_keys if isinstance(key, str) and key}
    if not processed:
        return True
    try:
        original = queue_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except OSError as exc:
        logger.warning("Could not read Scout queue for acknowledgement %s: %s", queue_path, exc)
        return False

    remaining = []
    removed = 0
    for raw_line in original.splitlines():
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line)
            key = scout_listing_key(row) if isinstance(row, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            key = None
        if key in processed:
            removed += 1
        else:
            remaining.append(raw_line)

    if not removed:
        return True

    temp_name = None
    try:
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=queue_path.parent,
            prefix=f".{queue_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            if remaining:
                temp_file.write("\n".join(remaining) + "\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, queue_path)
        return True
    except OSError as exc:
        logger.warning("Could not rewrite Scout queue %s: %s", queue_path, exc)
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass
        return False
