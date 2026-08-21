import sounddevice as sd


def refresh_devices():
    sd._terminate()
    sd._initialize()


def get_devices():
    """List of input devices (WASAPI only), sorted alphabetically.
    ASCII names come before non-ASCII (e.g. Cyrillic).
    """
    refresh_devices()

    hostapis = sd.query_hostapis()
    devices = []

    for idx, dev in enumerate(sd.query_devices()):

        if dev["max_input_channels"] == 0:
            continue

        driver = hostapis[dev["hostapi"]]["name"]

        if "WASAPI" not in driver:
            continue

        devices.append(
            {
                "id": idx,
                "name": dev["name"],
                "driver": driver.replace("Windows ", ""),
                "channels": dev["max_input_channels"],
                "samplerate": int(dev["default_samplerate"]),
            }
        )

    return sorted(
        devices,
        key=lambda d: (
            not d["name"][0].isascii(),
            d["name"].casefold(),
        ),
    )


def find_device_by_name(saved_name):
    """Finds device by name saved in config.

    Returns device dict (as in get_devices()) or None if device with
    such name is not currently connected/not found.
    """
    for dev in get_devices():
        if dev["name"] == saved_name:
            return dev
    return None


def _wasapi_default_input_id():
    """ID of device that WASAPI hostapi itself considers default input.

    More reliable than sd.default.device[0] — that global PortAudio default
    may point to device in ANOTHER host-API (not WASAPI), and then its id
    simply won't be found in our WASAPI-only device list, causing selection
    to constantly fall back to "first alphabetically".
    """
    for api in sd.query_hostapis():
        if "WASAPI" in api["name"]:
            default_id = api.get("default_input_device", -1)
            if default_id is not None and default_id >= 0:
                return default_id
    return None


def pick_default_from(devices):
    """Selects system default device FROM ALREADY OBTAINED list
    devices (without re-querying audio system via get_devices()).

    Preference order: WASAPI default -> global PortAudio default ->
    first device in list (extreme case if nothing could be matched).
    """
    if not devices:
        return None

    wasapi_default_id = _wasapi_default_input_id()
    if wasapi_default_id is not None:
        match = next((d for d in devices if d["id"] == wasapi_default_id), None)
        if match is not None:
            return match

    default_id = sd.default.device[0]
    return next((d for d in devices if d["id"] == default_id), devices[0])


def get_system_default_device():
    """Returns system default input device (or first from list if system
    default for some reason isn't in filtered WASAPI device list).
    Does its own device query — for cases when fresh list isn't available yet
    (see pick_default_from for reusing already obtained list).
    """
    return pick_default_from(get_devices())


def resolve_device(saved_name):
    """Determines which device to use on program start.

    If saved_name is found among current devices — use it.
    If not (device disconnected/renamed) — fall back to system default
    input device and report this via warning (second element in returned tuple).

    Returns (device_dict, warning_message_or_None).
    """
    if saved_name:
        device = find_device_by_name(saved_name)
        if device is not None:
            return device, None

    default_device = get_system_default_device()

    if default_device is None:
        raise RuntimeError("No suitable input device found.")

    warning = None
    if saved_name:
        warning = (
            f"Saved device '{saved_name}' not found. "
            f"Using default device: '{default_device['name']}'."
        )

    return default_device, warning


def print_devices():
    print("=== Devices ===\n")

    print(
        f"{'ID':<4}"
        f"{'Device':<40}"
        f"{'Driver':<12}"
        f"{'Ch':>4}"
        f"{'Rate':>10}"
    )

    print("-" * 72)

    for dev in get_devices():
        print(
            f"{dev['id']:<4}"
            f"{dev['name']:<40.40}"
            f"{dev['driver']:<12}"
            f"{dev['channels']:>4}"
            f"{dev['samplerate']:>10}"
        )


if __name__ == "__main__":
    print_devices()
