import json
import os
import uuid

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ---- Дефолты ----
IDLE_TIMEOUT_DEFAULT = 0.3          # сек, задержка перед переходом в idle
BLINK_INTERVAL_MIN_DEFAULT = 2.0    # сек
BLINK_INTERVAL_MAX_DEFAULT = 6.0    # сек
BLINK_DURATION_DEFAULT_MS = 150     # мс

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
    """Полная структура конфига со всеми дефолтами. Используется при первом
    запуске (файла ещё нет) и как основа для докладывания недостающих ключей
    в уже существующий, но неполный конфиг (например после апдейта проекта).
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
    }


def new_state(loud_db=None, pitch_hz=None):
    """Создаёт новый черновик состояния. name/normal заполняются пользователем
    в UI и обязательны для того, чтобы черновик стал полноценным состоянием
    (см. is_state_complete). loud_db/pitch_hz — координата точки/порога,
    в зависимости от режима могут быть не обе заданы.
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
    """Состояние можно сохранить, только если заполнены имя и normal-картинка."""
    return bool(state.get("name", "").strip()) and bool(state.get("normal"))


def _merge_defaults(loaded, defaults):
    """Рекурсивно докладывает отсутствующие ключи из defaults в loaded,
    не трогая уже существующие значения. Нужно, чтобы старый config.json
    (сохранённый до появления новых полей, например после удаления ASIO)
    не ломал программу KeyError'ами.
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
    """Загружает конфиг с диска. Если файла нет или он повреждён — создаёт
    новый с дефолтами и сразу сохраняет его на диск.
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
