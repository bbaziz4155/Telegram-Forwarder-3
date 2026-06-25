"""
Auto-resume state — persists the active copy-job to disk so the bot can
restart it automatically if the process is killed (e.g. Replit going to sleep).

Lifecycle
---------
  save_resume()   called in _launch_job() the moment a copy task is created
  clear_resume()  called in _run_copy() finally — runs on normal finish OR
                  user /stopjob cancel; does NOT run if the process is killed,
                  which is exactly when we want the file to survive.
  load_resume()   called once in schedule_auto_resume() at startup.
  claim_resume()  atomic version of load_resume() — renames autoresume.json
                  to autoresume.running.json so a second process starting
                  concurrently cannot also claim the same job (prevents
                  duplicate copies when the bot restarts more than once in
                  quick succession).
"""
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

# Respect DATA_DIR so autoresume.json lives on the same persistent volume
# as channel_settings.json and checkpoints/ (prevents stale-channel on redeploy).
_DATA_DIR    = os.environ.get("DATA_DIR",
               os.path.join(os.path.dirname(__file__), "..", "data"))
_RESUME_FILE  = os.path.join(_DATA_DIR, "autoresume.json")
_RUNNING_FILE = os.path.join(_DATA_DIR, "autoresume.running.json")

# A .running.json older than this is considered orphaned (the process that
# claimed it must have crashed during the 20-second countdown before the job
# even started).  On the next restart we recover it so the job can re-run.
_CLAIM_TIMEOUT_SECS = 120


def save_resume(chat_id: int, src, dst, opts: dict) -> None:
    """Persist the running job so it can be restarted after a process kill."""
    payload = {
        "chat_id": chat_id,
        # src/dst can be int (channel ID) or str (@username) — keep as-is
        "src": src,
        "dst": dst,
        "opts": {
            # sets are not JSON-serializable — store as sorted list
            "allowed_exts":        sorted(opts.get("allowed_exts") or []),
            "caption_replacement": opts.get("caption_replacement", ""),
            "caption_suffix":      opts.get("caption_suffix", ""),
            "notify_every":        opts.get("notify_every", 0),
            "skip_text":           bool(opts.get("skip_text", False)),
            "rate_delay":          float(opts.get("rate_delay", 0.0)),
            "filter_label":        opts.get("filter_label", "ALL"),
        },
    }
    try:
        os.makedirs(os.path.dirname(_RESUME_FILE), exist_ok=True)
        with open(_RESUME_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        logger.info("Auto-resume state saved (chat_id=%s, src=%s, dst=%s)", chat_id, src, dst)
    except Exception as e:
        logger.warning("Auto-resume: could not save state: %s", e)


def clear_resume() -> None:
    """
    Delete the auto-resume state (both autoresume.json AND autoresume.running.json).
    Called when a job ends cleanly (finish or user cancel) so it does not
    re-trigger on the next restart.
    """
    for path in (_RESUME_FILE, _RUNNING_FILE):
        try:
            os.remove(path)
            logger.info("Auto-resume state cleared (%s).", os.path.basename(path))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Auto-resume: could not clear %s: %s", os.path.basename(path), e)


def claim_resume() -> dict | None:
    """
    Atomically claim the auto-resume state for this process.

    Uses an OS-level atomic rename so that two bot processes starting in
    quick succession cannot both claim the same job and start duplicate copy
    tasks.  The first process to rename autoresume.json → autoresume.running.json
    wins; the second process finds the file already gone and returns None.

    Crash recovery: if a previous process claimed the file but crashed before
    the job started (e.g. during the 20-second countdown), the .running.json
    is left orphaned.  On the next startup we detect it by mtime and recover
    it back to autoresume.json so the job can be retried.

    Returns the resume dict (with opts.allowed_exts as a set), or None.
    """
    os.makedirs(_DATA_DIR, exist_ok=True)

    # ── Step 1: recover any stale orphaned .running.json ──────────────────────
    if os.path.exists(_RUNNING_FILE):
        try:
            age = time.time() - os.path.getmtime(_RUNNING_FILE)
            if age > _CLAIM_TIMEOUT_SECS:
                logger.info(
                    "Auto-resume: found stale .running.json (%.0fs old) — recovering.", age
                )
                # Rename back; if autoresume.json also somehow exists, remove
                # the stale running file so we don't double-up.
                if os.path.exists(_RESUME_FILE):
                    os.remove(_RUNNING_FILE)
                else:
                    os.rename(_RUNNING_FILE, _RESUME_FILE)
        except Exception as e:
            logger.warning("Auto-resume: could not recover .running.json: %s", e)

    # ── Step 2: atomic claim ───────────────────────────────────────────────────
    try:
        os.rename(_RESUME_FILE, _RUNNING_FILE)
    except FileNotFoundError:
        # Nothing to claim (file was never written, already claimed by
        # another process, or explicitly cleared by a previous run).
        return None
    except Exception as e:
        logger.warning("Auto-resume: claim failed unexpectedly: %s", e)
        return None

    # ── Step 3: read the claimed file ─────────────────────────────────────────
    try:
        with open(_RUNNING_FILE) as f:
            data = json.load(f)
        data["opts"]["allowed_exts"] = set(data["opts"].get("allowed_exts", []))
        logger.info(
            "Auto-resume: claimed job (src=%s, dst=%s)", data.get("src"), data.get("dst")
        )
        return data
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Auto-resume: malformed claimed state (%s) — discarding", e)
        try:
            os.remove(_RUNNING_FILE)
        except Exception:
            pass
        return None


def load_resume() -> dict | None:
    """
    Return the saved job state dict, or None if no resume is pending.
    Use claim_resume() at startup instead — it prevents duplicate jobs when
    the bot restarts more than once in quick succession.  load_resume() is
    kept for callers that only need to inspect state without claiming it.

    Returned dict shape:
      {
        "chat_id": int,
        "src":     int | str,
        "dst":     int | str,
        "opts": {
          "allowed_exts":        set[str],
          "caption_replacement": str,
          "notify_every":        int,
          "skip_text":           bool,
          "rate_delay":          float,
          "filter_label":        str,
        }
      }
    """
    try:
        with open(_RESUME_FILE) as f:
            data = json.load(f)
        # Convert list back to set for the engine
        data["opts"]["allowed_exts"] = set(data["opts"].get("allowed_exts", []))
        return data
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("Auto-resume: malformed state file (%s) — ignoring", e)
        clear_resume()
        return None
