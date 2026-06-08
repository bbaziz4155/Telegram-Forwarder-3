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
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

_RESUME_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "autoresume.json")


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
    Delete the auto-resume state.
    Called when a job ends cleanly (finish or user cancel) so it does not
    re-trigger on the next restart.
    """
    try:
        os.remove(_RESUME_FILE)
        logger.info("Auto-resume state cleared.")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Auto-resume: could not clear state: %s", e)


def load_resume() -> dict | None:
    """
    Return the saved job state dict, or None if no resume is pending.

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
