import logging

from config import load_config, save_config, MODE_COMBO
from list_device import resolve_device
from audio_process import AudioEngine
from state_engine import StateSelector
from avatar_window import AvatarWindow
from control_panel import ControlPanel

logging.basicConfig(level=logging.INFO)

TICK_MS = 30            # как часто обновляем состояние аватара (в мс)
WATCHDOG_CHECK_MS = 1000  # как часто проверяем, жив ли аудио-поток (в мс)
WATCHDOG_TIMEOUT = 2.5    # сек без единого callback'а -> поток считается мёртвым
MAX_AUTO_RESTARTS = 3     # после стольки неудачных автовосстановлений подряд - перестаём пытаться сами


def main():
    config = load_config()

    device, warning = resolve_device(config.get("device_name"))
    config["device_name"] = device["name"]

    # Движок и текущее устройство хранятся в изменяемых контейнерах: при
    # смене устройства (вручную или автоматически через watchdog) их нужно
    # пересоздавать "на лету" — обычная переменная не позволила бы
    # замыканиям (get_live, tick, watchdog) увидеть новый объект.
    engine_box = [AudioEngine(device_name=device["id"])]
    engine_box[0].start()
    current_device_box = [device]

    watchdog_state = {"consecutive_failures": 0, "error_shown": False}

    selector = StateSelector(idle_timeout=config.get("idle_timeout", 0.3))

    def get_live():
        return engine_box[0].get_latest()

    def _swap_engine(new_device):
        """Останавливает текущий поток и запускает новый на new_device.
        Используется и при ручном выборе устройства в панели, и при
        автоматическом восстановлении через watchdog. Возвращает True/False.
        """
        old_engine = engine_box[0]
        try:
            new_engine = AudioEngine(device_name=new_device["id"])
            new_engine.start()
        except Exception:
            logging.exception("Не удалось запустить устройство '%s'", new_device["name"])
            return False

        engine_box[0] = new_engine
        current_device_box[0] = new_device
        old_engine.stop()
        config["device_name"] = new_device["name"]
        return True

    def on_device_selected(new_device):
        """Вызывается панелью сразу при выборе устройства в списке — без
        ожидания нажатия 'Сохранить'. Если новое устройство не открылось —
        старый поток остаётся работать, пользователю показывается ошибка,
        а выбор в выпадающем списке откатывается обратно на реально
        активное устройство (иначе UI показывал бы не то, что играет).
        """
        previous_name = current_device_box[0]["name"]

        if _swap_engine(new_device):
            watchdog_state["consecutive_failures"] = 0
            watchdog_state["error_shown"] = False
        else:
            panel.notify_engine_error(
                f"Не удалось открыть устройство '{new_device['name']}'. "
                f"Продолжаю использовать '{previous_name}'."
            )
            panel.set_selected_device_silently(previous_name)

    panel = ControlPanel(config, get_live=get_live, on_device_selected=on_device_selected)
    avatar = AvatarWindow(config, master=panel)

    if warning:
        panel.notify_engine_error(warning)

    def check_watchdog():
        if avatar.is_closed():
            return  # tick() уже вызовет _shutdown, не дублируем работу

        if engine_box[0].is_alive(timeout=WATCHDOG_TIMEOUT):
            watchdog_state["consecutive_failures"] = 0
            watchdog_state["error_shown"] = False
        elif watchdog_state["consecutive_failures"] < MAX_AUTO_RESTARTS:
            watchdog_state["consecutive_failures"] += 1
            logging.warning(
                "Аудио-поток не отвечает (попытка восстановления %d/%d) — "
                "перезапускаю устройство '%s'",
                watchdog_state["consecutive_failures"], MAX_AUTO_RESTARTS,
                current_device_box[0]["name"],
            )
            _swap_engine(current_device_box[0])
        elif not watchdog_state["error_shown"]:
            watchdog_state["error_shown"] = True
            panel.notify_engine_error(
                f"Устройство '{current_device_box[0]['name']}' не отвечает после "
                f"нескольких попыток восстановления. Выберите устройство заново "
                f"в списке."
            )

        panel.after(WATCHDOG_CHECK_MS, check_watchdog)

    def tick():
        if avatar.is_closed():
            _shutdown()
            return

        rms, pitch = get_live()

        try:
            selector.idle_timeout = float(panel.idle_timeout_var.get())
        except ValueError:
            pass

        states = config["states"][config.get("mode", MODE_COMBO)]
        active_state = selector.update(states, rms, pitch)

        avatar.render(active_state)

        panel.after(TICK_MS, tick)

    def _shutdown():
        engine_box[0].stop()
        save_config(config)
        panel.destroy()

    panel.protocol("WM_DELETE_WINDOW", _shutdown)

    panel.after(TICK_MS, tick)
    panel.after(WATCHDOG_CHECK_MS, check_watchdog)
    panel.mainloop()


if __name__ == "__main__":
    main()
