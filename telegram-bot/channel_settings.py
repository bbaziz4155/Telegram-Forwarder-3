"""
Persistent source/destination channel overrides.

Values set via /setsource and /setdest are saved to data/channel_settings.json
and loaded at startup so they survive Railway redeploys without touching env vars.
"""
import json
import logging
import os

import config

logger = logging.getLogger(__name__)


def _path() -> str:
    data_dir = os.environ.get(
        "DATA_DIR", os.path.join(os.path.dirname(__file__), "data")
    )
    return os.path.join(data_dir, "channel_settings.json")


def load():
    """Read saved overrides and apply them to the config module at startup."""
    try:
        with open(_path()) as f:
            data = json.load(f)
        if data.get("source"):
            config.SOURCE_CHANNEL = int(data["source"])
        if data.get("dest"):
            config.DEST_CHANNEL = int(data["dest"])
        logger.info(
            "Channel overrides loaded: source=%s dest=%s",
            config.SOURCE_CHANNEL, config.DEST_CHANNEL,
        )
    except FileNotFoundError:
        pass  # no overrides saved yet — use env/config defaults
    except Exception as e:
        logger.warning("Could not load channel_settings.json: %s", e)


def save():
    """Write the current config channel IDs to disk.

    Merges with the existing file so /setsource never overwrites a saved
    dest with 0 (and vice-versa).  Only non-zero values are written.
    """
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existing: dict = {}
    try:
        with open(path) as _rf:
            existing = json.load(_rf)
    except Exception:
        pass
    if config.SOURCE_CHANNEL:
        existing["source"] = config.SOURCE_CHANNEL
    if config.DEST_CHANNEL:
        existing["dest"] = config.DEST_CHANNEL
    try:
        with open(path, "w") as f:
            json.dump(existing, f)
    except Exception as e:
        logger.warning("Could not save channel_settings.json: %s", e)
