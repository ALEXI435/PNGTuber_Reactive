import json
import os
import sys
import uuid

# Determine config path (works in both script and exe mode)
if getattr(sys, 'frozen', False):
    # Running as exe (PyInstaller)
    exe_dir = os.path.dirname(sys.executable)
    CONFIG_PATH = os.path.join(exe_dir, "config.json")
else:
    # Running as script
    CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ---- Defaults ----
IDLE_TIMEOUT_DEFAULT = 0.3          # sec, delay before idle transition
BLINK_INTERVAL_MIN_DEFAULT = 2.0    # sec
BLINK_INTERVAL_MAX_DEFAULT = 6.0    # sec
BLINK_DURATION_DEFAULT_MS = 150     # ms

MAX_STATES = 10

MODE_VOLUME = "volume"
MODE_PITCH = "pitch"
MODE_COMBO = "combo"

LANG_RU = "ru"
LANG_EN = "en"


def _default_blink_block():
    return {
        "normal": None,
        "blink": None,
        "blink_interval_min": BLINK_INTERVAL_MIN_DEFAULT,
        "blink_interval_max": BLINK_INTERVAL_MAX_DEFAULT,
        "blink_duration_ms": BLINK_DURATION_DEFAULT_MS,
    }


def default_config():
    """Full config structure with all defaults. Used on first run (no file yet)
    and as base for adding missing keys to existing config (e.g. after update).
    """
    return {
        "device_name": None,
        "language": LANG_EN,
        "mode": MODE_VOLUME,
        "idle_timeout": IDLE_TIMEOUT_DEFAULT,
        "idle": _default_blink_block(),
        "states": {
            MODE_VOLUME: [],
            MODE_PITCH: [],
            MODE_COMBO: [],
        },
        "avatar_window": {
            "x": 100,
            "y": 100,
            "width": 400,
            "height": 400,
        },
        "avatar_view": {
            "zoom": 1.0,
            "pan_x": 0,
            "pan_y": 0,
        },
        "shaking": {
            "enabled": False,
            "amplitude": 10,
            "speed": 5,
        },
    }


def new_state(loud_db=None, pitch_hz=None):
    """Creates a new state draft. name/normal are filled by user in UI and are
    required for the draft to become a full state (see is_state_complete).
    loud_db/pitch_hz — coordinate/threshold, depending on mode both may not be set.
    """
    state = _default_blink_block()
    state.update(
        {
            "id": uuid.uuid4().hex,
            "name": "",
            "loud_db": loud_db,
            "pitch_hz": pitch_hz,
        }
    )
    return state


def is_state_complete(state):
    """State can be saved only if name and normal image are filled."""
    return bool(state.get("name", "").strip()) and bool(state.get("normal"))


def _merge_defaults(loaded, defaults):
    """Recursively adds missing keys from defaults to loaded, without touching
    existing values. Needed so old config.json (saved before new fields, e.g. after ASIO removal)
    doesn't break the program with KeyErrors.
    """
    if not isinstance(loaded, dict):
        return defaults

    result = dict(loaded)
    for key, default_value in defaults.items():
        if key not in result:
            result[key] = default_value
        elif isinstance(default_value, dict):
            result[key] = _merge_defaults(result[key], default_value)
    return result


def load_config(path=CONFIG_PATH):
    """Loads config from disk. If file doesn't exist or is corrupted — creates
    new one with defaults and saves it immediately.
    """
    if not os.path.exists(path):
        config = default_config()
        save_config(config, path)
        return config

    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
    except (json.JSONDecodeError, OSError):
        config = default_config()
        save_config(config, path)
        return config

    config = _merge_defaults(loaded, default_config())
    return config


def save_config(config, path=CONFIG_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def can_add_state(config, mode):
    return len(config["states"][mode]) < MAX_STATES


def auto_save(config):
    """Automatically saves config to disk. Call after any configuration change."""
    save_config(config)
