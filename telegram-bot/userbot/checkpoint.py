"""
Checkpoint manager — saves & loads copy progress so jobs can be resumed
after a restart, crash, or Ctrl+C.

Also stores the set of already-copied source message IDs so that re-running
/copy on a channel that was already copied skips duplicates automatically.

Checkpoint file location:
    telegram-bot/data/checkpoints/<source_id>_<dest_id>.json
"""
import json
import os
import time

CHECKPOINTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "checkpoints"
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
    Converts the 'copied_ids' set to a sorted list for JSON serialisation."""
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    on_disk = dict(state)
    on_disk["copied_ids"] = sorted(state.get("copied_ids", set()))
    with open(_path(source_id, dest_id), "w") as f:
        json.dump(on_disk, f, indent=2)


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
