import sounddevice as sd


def refresh_devices():
    sd._terminate()
    sd._initialize()


def get_devices():
    """Список входных устройств (только WASAPI), отсортированный по алфавиту.
    ASCII-имена идут раньше не-ASCII (например кириллических).
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
    """Ищет устройство по сохранённому в конфиге имени.

    Возвращает словарь устройства (как в get_devices()) или None, если
    устройство с таким именем сейчас не подключено/не найдено.
    """
    for dev in get_devices():
        if dev["name"] == saved_name:
            return dev
    return None


def _wasapi_default_input_id():
    """ID устройства, которое сам WASAPI-хостапи считает входом по умолчанию.

    Надёжнее, чем sd.default.device[0] — тот глобальный дефолт PortAudio
    может указывать на устройство в ДРУГОМ хост-API (не WASAPI), и тогда
    его id просто не найдётся в нашем WASAPI-only списке устройств, из-за
    чего выбор постоянно проваливался до "первого по алфавиту".
    """
    for api in sd.query_hostapis():
        if "WASAPI" in api["name"]:
            default_id = api.get("default_input_device", -1)
            if default_id is not None and default_id >= 0:
                return default_id
    return None


def pick_default_from(devices):
    """Выбирает системное устройство по умолчанию ИЗ УЖЕ ПОЛУЧЕННОГО списка
    devices (без повторного опроса звуковой системы через get_devices()).

    Порядок предпочтений: WASAPI-дефолт -> глобальный дефолт PortAudio ->
    первое устройство в списке (крайний случай, если вообще ничего не
    удалось сопоставить).
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
    """Возвращает системное устройство ввода по умолчанию (или первое из
    списка, если системный дефолт почему-то не входит в отфильтрованный
    список WASAPI-устройств). Делает собственный опрос устройств — для
    случаев, когда под рукой ещё нет свежего списка (см. pick_default_from
    для повторного использования уже полученного списка).
    """
    return pick_default_from(get_devices())


def resolve_device(saved_name):
    """Определяет, какое устройство использовать при старте программы.

    Если saved_name найден среди текущих устройств — используем его.
    Если нет (устройство отключено/переименовано) — откатываемся на
    системное устройство ввода по умолчанию и сообщаем об этом через
    warning (второй элемент в возвращаемом кортеже).

    Возвращает (device_dict, warning_message_or_None).
    """
    if saved_name:
        device = find_device_by_name(saved_name)
        if device is not None:
            return device, None

    default_device = get_system_default_device()

    if default_device is None:
        raise RuntimeError("Не найдено ни одного подходящего устройства ввода.")

    warning = None
    if saved_name:
        warning = (
            f"Сохранённое устройство '{saved_name}' не найдено. "
            f"Используется устройство по умолчанию: '{default_device['name']}'."
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
