"""
Checkpoint manager — saves & loads copy progress so jobs can be resumed
after a restart, crash, or Ctrl+C.

Also stores the set of already-copied source message IDs so that re-running
/copy on a channel that was already copied skips duplicates automatically.

Checkpoint file location:
    <DATA_DIR>/checkpoints/<source_id>_<dest_id>.json

DATA_DIR defaults to telegram-bot/data/ but can be overridden via the
DATA_DIR environment variable to point at a Railway Volume or any
other persistent mount.
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")
CHECKPOINTS_DIR = os.path.join(
    os.environ.get("DATA_DIR", _DEFAULT_DATA_DIR), "checkpoints"
)


def _path(source_id: int, dest_id: int) -> str:
    os.makedirs(CHECKPOINTS_DIR, exist_ok=True)
    return os.path.join(CHECKPOINTS_DIR, f"{source_id}_{dest_id}.json")


def load(source_id: int, dest_id: int) -> dict:
    """Return saved checkpoint dict, or a fresh one if none exists.
    Always ensures 'copied_ids' is a Python set for O(1) lookups."""
    p = _path(source_id, dest_id)
    if os.path.exists(p):
        try:
            with open(p) as f:
                data = json.load(f)
            # Deserialise the list back to a set
            data["copied_ids"] = set(data.get("copied_ids", []))
            return data
        except Exception:
            pass
    return {
        "source_id":   source_id,
        "dest_id":     dest_id,
        "last_msg_id": 0,
        "copied":      0,
        "skipped":     0,
        "failed":      0,
        "flood_waits": 0,
        "copied_ids":  set(),          # set of source msg IDs already sent
        "started_at":  time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at":  None,
    }


def save(source_id: int, dest_id: int, state: dict):
    """Persist checkpoint to disk.

    Only serialises IDs strictly greater than last_msg_id ('gap' IDs).
    IDs <= last_msg_id are implicitly covered by min_id on resume, so
    persisting the full set causes O(n) disk I/O that grows linearly with
    channel size — for an 830 K-message channel that is ~8 MB written every
    25 messages (~265 GB total I/O over a full run).  Gap IDs stay tiny:
    they are only the few album siblings above the current watermark.
    """
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    last_id = state.get("last_msg_id", 0)
    on_disk = dict(state)
    # Only keep IDs above the watermark.  Everything <= last_msg_id is
    # redundant because copy_channel_files uses min_id=last_msg_id on
    # resume, which already skips those messages entirely.
    gap_ids = sorted(i for i in state.get("copied_ids", set()) if i > last_id)
    on_disk["copied_ids"] = gap_ids
    try:
        with open(_path(source_id, dest_id), "w") as f:
            json.dump(on_disk, f, indent=2)
    except Exception as e:
        logger.warning(
            "Checkpoint save failed (%s_%s) — progress may be lost on crash: %s",
            source_id, dest_id, e,
        )


def delete(source_id: int, dest_id: int):
    """Remove checkpoint (job completed cleanly)."""
    p = _path(source_id, dest_id)
    if os.path.exists(p):
        os.remove(p)


def exists(source_id: int, dest_id: int) -> bool:
    p = _path(source_id, dest_id)
    if not os.path.exists(p):
        return False
    cp = load(source_id, dest_id)
    return cp.get("last_msg_id", 0) > 0
