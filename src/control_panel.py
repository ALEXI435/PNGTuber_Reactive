import logging
import time
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

logger = logging.getLogger("control_panel")

from config import (
    MAX_STATES,
    MODE_COMBO,
    MODE_PITCH,
    MODE_VOLUME,
    LANG_EN,
    LANG_RU,
    BLINK_DURATION_DEFAULT_MS,
    BLINK_INTERVAL_MAX_DEFAULT,
    BLINK_INTERVAL_MIN_DEFAULT,
    IDLE_TIMEOUT_DEFAULT,
    is_state_complete,
    new_state,
    auto_save,
)
from list_device import get_devices, pick_default_from
from scale_geometry import (
    DB_MAJOR_STEP,
    DB_MAX,
    DB_MIN,
    DB_MINOR_STEP,
    HZ_MAJOR_STEP,
    HZ_MAX,
    HZ_MIN,
    HZ_MINOR_STEP,
    db_to_x,
    hz_to_x,
    hz_to_y,
    ticks,
    x_to_db,
    x_to_hz,
    y_to_hz,
)
from state_engine import rms_to_db

HIT_RADIUS = 10
POINT_RADIUS = 8
POINT_RADIUS_SELECTED = 12

COLOR_LOUD = "#1D9E75"
COLOR_PITCH = "#7F77DD"
COLOR_LIVE = "#378ADD"
COLOR_INTERSECT = "#FFC94D"
COLOR_POINT = "#5B8DEF"
COLOR_TICK = "#666666"
COLOR_TEXT_MUTED = "#999999"
COLOR_SAVE_GREEN = "#2E8B4F"
COLOR_SAVE_GREEN_HOVER = "#256F3F"

MIN_WINDOW_WIDTH = 700
MIN_WINDOW_HEIGHT = 960

# Параметры сглаживания визуальных индикаторов
LIVE_SMOOTHING_ALPHA = 0.3  # EMA для громкости (dB)
LIVE_SMOOTHING_ALPHA_UP_PITCH = 0.6  # Быстрый рост для pitch
LIVE_SMOOTHING_ALPHA_DOWN_PITCH = 0.15  # Падение для pitch


# ---- Переводы видимого текста интерфейса (внутренние ключи JSON не трогаем) ----
STRINGS = {
    "device_label": {"ru": "Устройство ввода", "en": "Input device"},
    "language_label": {"ru": "Язык", "en": "Language"},
    "mode_label": {"ru": "Режим детекции", "en": "Detection mode"},
    "mode_volume": {"ru": "Громкость", "en": "Volume"},
    "mode_pitch": {"ru": "Тон", "en": "Pitch"},
    "mode_combo": {"ru": "Громкость + тон", "en": "Volume + Pitch"},
    "idle_timeout_label": {"ru": "Таймаут перед idle, сек", "en": "Timeout before idle, sec"},
    "add_hint": {"ru": "Кликните по пустой области, чтобы добавить состояние", "en": "Click empty area to add a state"},
    "name_label": {"ru": "Имя точки:", "en": "Point name:"},
    "normal_label": {"ru": "обычная", "en": "normal"},
    "blink_label": {"ru": "моргание", "en": "blink"},
    "not_set": {"ru": "не указано", "en": "not set"},
    "interval_label": {"ru": "Интервал, сек:", "en": "Interval, sec:"},
    "duration_label": {"ru": "Длительность, мс:", "en": "Duration, ms:"},
    "save_btn": {"ru": "Сохранить", "en": "Save"},
    "delete_btn": {"ru": "Удалить", "en": "Delete"},
    "save_config_btn": {"ru": "Сохранить конфигурацию", "en": "Save configuration"},
    "idle_section": {"ru": "Idle", "en": "Idle"},
    "limit_title": {"ru": "Достигнут лимит", "en": "Limit reached"},
    "limit_message": {
        "ru": f"Уже настроено максимальное количество состояний ({MAX_STATES}). Удалите одно из существующих, чтобы добавить новое.",
        "en": f"Maximum number of states already configured ({MAX_STATES}). Delete an existing one to add a new one.",
    },
    "device_warning_title": {"ru": "Устройство не найдено", "en": "Device not found"},
    "saved_title": {"ru": "Сохранено", "en": "Saved"},
    "saved_message": {"ru": "Конфигурация сохранена.", "en": "Configuration saved."},
    "engine_error_title": {"ru": "Ошибка устройства", "en": "Device error"},
    "experimental_warning": {
        "ru": "⚠ Экспериментальная функция. Может работать не стабильно",
        "en": "⚠ Experimental feature. May not work stably",
    },
}


def _t(config, key):
    lang = config.get("language", LANG_RU)
    return STRINGS.get(key, {}).get(lang, key)


def _digits_only_validate(value):
    return value == "" or value.isdigit()


class StateScaleCanvas(tk.Canvas):
    """Общий Canvas-виджет и для 1D-шкал (только дБ или только Гц), и для
    2D-плоскости (комбо). Режим задаётся axis_mode: "db", "hz" или "combo".
    """

    def __init__(self, parent, axis_mode, states_list_ref, on_select, on_limit_reached):
        self.axis_mode = axis_mode
        self.states_list_ref = states_list_ref  # прямая ссылка на config["states"][mode]
        self.on_select = on_select
        self.on_limit_reached = on_limit_reached

        if axis_mode == "combo":
            width, height = 620, 260
            self.track_x_min, self.track_x_max = 40, 580
            self.track_y_min, self.track_y_max = 20, 220
        else:
            width, height = 620, 110
            self.track_min_px, self.track_max_px = 40, 580

        super().__init__(parent, width=width, height=height, bg="#1a1a1a", highlightthickness=0)

        self.selected_state = None
        self.draft_state = None
        self._dragging = False

        # Сырые значения (входящие)
        self.live_db = DB_MIN
        self.live_pitch = HZ_MIN
        
        # Сглаженные значения для отображения
        self._smoothed_db = DB_MIN
        self._smoothed_pitch = HZ_MIN
        
        # Для линейного затухания dB (равномерное падение)
        self._db_decay_start = None
        self._db_decay_start_value = None
        
        # Для линейного затухания dB (равномерное падение)
        self._db_decay_start = None
        self._db_decay_start_value = None

        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)

        self.redraw()

    # ---- координаты <-> значения ----

    def _point_to_px(self, state):
        if self.axis_mode == "db":
            return db_to_x(state.get("loud_db", DB_MIN), self.track_min_px, self.track_max_px), 45
        if self.axis_mode == "hz":
            return hz_to_x(state.get("pitch_hz", HZ_MIN), self.track_min_px, self.track_max_px), 45
        x = db_to_x(state.get("loud_db", DB_MIN), self.track_x_min, self.track_x_max)
        y = hz_to_y(state.get("pitch_hz", HZ_MIN), self.track_y_min, self.track_y_max)
        return x, y

    def _px_to_values(self, x, y):
        if self.axis_mode == "db":
            return x_to_db(x, self.track_min_px, self.track_max_px), None
        if self.axis_mode == "hz":
            return None, x_to_hz(x, self.track_min_px, self.track_max_px)
        db = x_to_db(x, self.track_x_min, self.track_x_max)
        hz = y_to_hz(y, self.track_y_min, self.track_y_max)
        return db, hz

    # ---- события мыши ----

    def _find_hit(self, x, y):
        all_states = list(self.states_list_ref)
        if self.draft_state is not None and self.draft_state not in all_states:
            all_states.append(self.draft_state)

        for state in all_states:
            px, py = self._point_to_px(state)
            if (px - x) ** 2 + (py - y) ** 2 <= HIT_RADIUS**2:
                return state
        return None

    def _discard_incomplete_draft(self, keep=None):
        """Удаляет незавершённый черновик, ЕСЛИ он не совпадает с keep.

        Раньше здесь был баг: функция стирала черновик безусловно, даже если
        пользователь кликнул ПО САМОМУ черновику (просто выбирая его снова,
        не создавая новый). Из-за этого точка визуально пропадала — объект
        оставался в selected_state, но переставал существовать и в
        states_list_ref, и в draft_state, поэтому redraw() его не рисовал.
        """
        if self.draft_state is None:
            return
        if self.draft_state is keep:
            return
        if not is_state_complete(self.draft_state):
            if self.selected_state is self.draft_state:
                self.selected_state = None
            self.draft_state = None

    def _on_press(self, event):
        hit = self._find_hit(event.x, event.y)

        if hit is not None:
            self._discard_incomplete_draft(keep=hit)
            self.selected_state = hit
            self._dragging = hit
        else:
            self._discard_incomplete_draft()

            if len(self.states_list_ref) >= MAX_STATES:
                self.on_limit_reached()
                self.redraw()
                return

            db, hz = self._px_to_values(event.x, event.y)
            draft = new_state(loud_db=db, pitch_hz=hz)
            self.draft_state = draft
            self.selected_state = draft
            self._dragging = draft

        self.on_select(self.selected_state, is_draft=(self.selected_state is self.draft_state))
        self.redraw()

    def _on_drag(self, event):
        if not self._dragging:
            return

        x = max(self._x_min(), min(self._x_max(), event.x))
        y = max(self._y_min(), min(self._y_max(), event.y))

        db, hz = self._px_to_values(x, y)
        if db is not None:
            self._dragging["loud_db"] = db
        if hz is not None:
            self._dragging["pitch_hz"] = hz

        self.redraw()
        if self.on_select is not None:
            self.on_select(self.selected_state, is_draft=(self.selected_state is self.draft_state), live_drag=True)

    def _on_release(self, event):
        self._dragging = False

    def _x_min(self):
        return self.track_x_min if self.axis_mode == "combo" else self.track_min_px

    def _x_max(self):
        return self.track_x_max if self.axis_mode == "combo" else self.track_max_px

    def _y_min(self):
        return self.track_y_min if self.axis_mode == "combo" else 0

    def _y_max(self):
        return self.track_y_max if self.axis_mode == "combo" else 110

    # ---- публичное API ----

    def set_live(self, live_db, live_pitch):
        """Устанавливает текущие значения с сглаживанием."""
        # Простое EMA сглаживание для dB
        self._smoothed_db = LIVE_SMOOTHING_ALPHA * live_db + (1 - LIVE_SMOOTHING_ALPHA) * self._smoothed_db
        
        # Адаптивное EMA для pitch
        if live_pitch > self._smoothed_pitch:
            alpha = LIVE_SMOOTHING_ALPHA_UP_PITCH
        else:
            alpha = LIVE_SMOOTHING_ALPHA_DOWN_PITCH
        self._smoothed_pitch = alpha * live_pitch + (1 - alpha) * self._smoothed_pitch
        
        # Сохраняем сырые значения для совместимости
        self.live_db = live_db
        self.live_pitch = live_pitch
        
        self.redraw()

    def deselect(self):
        self._discard_incomplete_draft()
        self.selected_state = None
        self.redraw()

    def remove_state(self, state):
        if state in self.states_list_ref:
            self.states_list_ref.remove(state)
        if self.draft_state is state:
            self.draft_state = None
        if self.selected_state is state:
            self.selected_state = None
        self.redraw()

    def commit_draft(self):
        """Черновик заполнен (имя+normal) -> добавляем в реальный список."""
        if self.draft_state is not None and self.draft_state not in self.states_list_ref:
            self.states_list_ref.append(self.draft_state)
        self.draft_state = None

    # ---- отрисовка ----

    def redraw(self):
        self.delete("all")
        if self.axis_mode == "combo":
            self._draw_combo()
        else:
            self._draw_1d()

    def _draw_1d(self):
        is_db = self.axis_mode == "db"
        vmin, vmax = (DB_MIN, DB_MAX) if is_db else (HZ_MIN, HZ_MAX)
        minor, major = (DB_MINOR_STEP, DB_MAJOR_STEP) if is_db else (HZ_MINOR_STEP, HZ_MAJOR_STEP)
        color = COLOR_LOUD if is_db else COLOR_PITCH
        unit = "дБ" if is_db else "Гц"

        # Используем сглаженные значения для отображения
        live_value = self._smoothed_db if is_db else self._smoothed_pitch
        live_x = (
            db_to_x(live_value, self.track_min_px, self.track_max_px)
            if is_db
            else hz_to_x(live_value, self.track_min_px, self.track_max_px)
        )

        self.create_rectangle(
            self.track_min_px, 38, live_x, 52, fill=COLOR_LIVE, outline="", stipple="gray50"
        )
        self.create_line(self.track_min_px, 45, self.track_max_px, 45, fill=color, width=4)

        self.create_line(self.track_min_px, 70, self.track_max_px, 70, fill=COLOR_TICK)
        for value, is_major in ticks(minor, major, vmin, vmax):
            x = (
                db_to_x(value, self.track_min_px, self.track_max_px)
                if is_db
                else hz_to_x(value, self.track_min_px, self.track_max_px)
            )
            h = 8 if is_major else 4
            self.create_line(x, 70 - h, x, 70 + h, fill=COLOR_TICK)
            if is_major:
                self.create_text(x, 88, text=str(int(value)), fill=COLOR_TEXT_MUTED, font=("Arial", 9))

        for state in self.states_list_ref:
            self._draw_point(state, color)
        if self.draft_state is not None and self.draft_state not in self.states_list_ref:
            self._draw_point(self.draft_state, color)

        if self.selected_state is not None:
            px, py = self._point_to_px(self.selected_state)
            value = self.selected_state.get("loud_db") if is_db else self.selected_state.get("pitch_hz")
            self._draw_tooltip(px, py + 22, f"{round(value)} {unit}")

    def _draw_combo(self):
        # Используем сглаженные значения для отображения
        live_x = db_to_x(self._smoothed_db, self.track_x_min, self.track_x_max)
        live_y = hz_to_y(self._smoothed_pitch, self.track_y_min, self.track_y_max)

        self.create_rectangle(
            self.track_x_min, self.track_y_min, live_x, self.track_y_max,
            fill=COLOR_LOUD, outline="", stipple="gray25",
        )
        self.create_rectangle(
            self.track_x_min, live_y, self.track_x_max, self.track_y_max,
            fill=COLOR_PITCH, outline="", stipple="gray25",
        )
        self.create_rectangle(
            self.track_x_min, live_y, live_x, self.track_y_max,
            fill=COLOR_INTERSECT, outline="", stipple="gray50",
        )

        self.create_line(live_x, self.track_y_min, live_x, self.track_y_max, fill=COLOR_LOUD, width=2)
        self.create_line(self.track_x_min, live_y, self.track_x_max, live_y, fill=COLOR_PITCH, width=2)
        self.create_oval(live_x - 6, live_y - 6, live_x + 6, live_y + 6, fill=COLOR_INTERSECT, outline="")

        self.create_line(self.track_x_min, self.track_y_max, self.track_x_max, self.track_y_max, fill=COLOR_TICK)
        self.create_line(self.track_x_min, self.track_y_min, self.track_x_min, self.track_y_max, fill=COLOR_TICK)

        for value, is_major in ticks(DB_MINOR_STEP, DB_MAJOR_STEP, DB_MIN, DB_MAX):
            x = db_to_x(value, self.track_x_min, self.track_x_max)
            h = 6 if is_major else 3
            self.create_line(x, self.track_y_max - h, x, self.track_y_max + h, fill=COLOR_TICK)
            if is_major:
                self.create_text(
                    x, self.track_y_max + 14, text=str(int(value)), fill=COLOR_TEXT_MUTED, font=("Arial", 9)
                )

        for value, is_major in ticks(HZ_MINOR_STEP, HZ_MAJOR_STEP, HZ_MIN, HZ_MAX):
            y = hz_to_y(value, self.track_y_min, self.track_y_max)
            w = 6 if is_major else 3
            self.create_line(self.track_x_min - w, y, self.track_x_min + w, y, fill=COLOR_TICK)
            if is_major:
                self.create_text(
                    self.track_x_min - 20, y, text=str(int(value)), fill=COLOR_TEXT_MUTED, font=("Arial", 8)
                )

        for state in self.states_list_ref:
            self._draw_point(state, COLOR_POINT)
        if self.draft_state is not None and self.draft_state not in self.states_list_ref:
            self._draw_point(self.draft_state, COLOR_POINT)

        if self.selected_state is not None:
            px, py = self._point_to_px(self.selected_state)
            db = self.selected_state.get("loud_db")
            hz = self.selected_state.get("pitch_hz")
            self._draw_tooltip(px, py + 26, f"{round(db)} дБ, {round(hz)} Гц")

    def _draw_point(self, state, color):
        px, py = self._point_to_px(state)
        selected = state is self.selected_state
        r = POINT_RADIUS_SELECTED if selected else POINT_RADIUS
        outline = "#ffffff" if selected else "#1a1a1a"
        width = 3 if selected else 2
        self.create_oval(px - r, py - r, px + r, py + r, fill=color, outline=outline, width=width)

    def _draw_tooltip(self, x, y, text):
        w = 8 * len(text) + 12
        self.create_rectangle(x - w / 2, y - 10, x + w / 2, y + 10, fill="#000000", outline="")
        self.create_text(x, y, text=text, fill="#ffffff", font=("Arial", 9))


class ControlPanel(ctk.CTk):
    def __init__(self, config, get_live=None, on_device_selected=None):
        super().__init__()
        self.config = config
        self.get_live = get_live or (lambda: (0.0, 0.0))
        self.on_device_selected = on_device_selected  # callback(device_dict) -> перезапуск движка

        self.title("PNGTube Ultimate")
        self.geometry(f"{MIN_WINDOW_WIDTH}x{MIN_WINDOW_HEIGHT}")

        self._translatable_widgets = []  # (widget, key, kwarg) для переключения языка
        self._suppress_device_callback = False

        self._build_device_section()
        self._build_mode_section()
        self._build_scale_frames()
        self._build_point_and_idle_container()
        self._build_save_button()

        # Считаем minsize по САМОМУ ТЯЖЁЛОМУ варианту разметки: комбо-график
        # (самый высокий из трёх) + одновременно панель точки И idle под ним
        # (idle съезжает вниз, когда точка выбрана - это самая высокая
        # суммарная комбинация). Раньше minsize считался ДО того, как вообще
        # упакован хоть один график и хоть одна из этих панелей, поэтому
        # получался сильно заниженным и не гарантировал, что всё влезет.
        self.scale_canvases[MODE_COMBO].pack(fill="x", padx=5, pady=5)
        self.point_frame.pack(fill="x", padx=10, pady=5)
        self.idle_frame.pack(fill="x", padx=10, pady=5)
        self.update_idletasks()
        self.minsize(MIN_WINDOW_WIDTH, self.winfo_reqheight())
        self.scale_canvases[MODE_COMBO].pack_forget()
        self.point_frame.pack_forget()
        self.idle_frame.pack_forget()

        self._select_mode(self.config.get("mode", MODE_COMBO))
        self._apply_language()

        self.after(50, self._tick)

    # ---- переводы ----

    def _tr(self, key):
        return _t(self.config, key)

    def _add_translatable(self, widget, key, kwarg="text"):
        self._translatable_widgets.append((widget, key, kwarg))

    def _apply_language(self):
        for widget, key, kwarg in self._translatable_widgets:
            try:
                widget.configure(**{kwarg: self._tr(key)})
            except Exception:
                pass
        self._refresh_image_buttons_text()
        self._refresh_idle_buttons()

    def _on_language_changed(self, value):
        self.config["language"] = LANG_RU if value == "RU" else LANG_EN
        self._apply_language()
        auto_save(self.config)

    # ---- секции UI ----

    def _build_device_section(self):
        frame = ctk.CTkFrame(self)
        frame.pack(fill="x", padx=10, pady=(10, 5))

        left = ctk.CTkFrame(frame, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        lbl = ctk.CTkLabel(left, text="")
        lbl.pack(anchor="w")
        self._add_translatable(lbl, "device_label")

        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x")

        self._devices = get_devices()
        names = [d["name"] for d in self._devices] or ["—"]

        saved_name = self.config.get("device_name")
        initial = saved_name if saved_name in names else (names[0] if names else "")

        self.device_var = tk.StringVar(value=initial)
        self.device_menu = ctk.CTkOptionMenu(
            row, variable=self.device_var, values=names, command=self._on_device_changed
        )
        self.device_menu.pack(side="left", fill="x", expand=True)

        ctk.CTkButton(row, text="⟳", width=32, command=self._refresh_devices).pack(side="left", padx=(5, 0))

        right = ctk.CTkFrame(frame, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        lang_lbl = ctk.CTkLabel(right, text="")
        lang_lbl.pack(anchor="w")
        self._add_translatable(lang_lbl, "language_label")

        current_lang = self.config.get("language", LANG_RU)
        self.language_var = tk.StringVar(value="RU" if current_lang == LANG_RU else "EN")
        lang_menu = ctk.CTkSegmentedButton(
            right, values=["RU", "EN"], variable=self.language_var, command=self._on_language_changed
        )
        lang_menu.pack(fill="x")

    def _current_device_dict(self, name):
        for d in self._devices:
            if d["name"] == name:
                return d
        return None

    def _notify_device_selected(self, name):
        logger.info("Пользователь выбрал устройство в списке: '%s'", name)

        device = self._current_device_dict(name)

        if device is None:
            # Устройства нет в закешированном self._devices (например список
            # успел устареть) - обновляем и пробуем ещё раз, вместо того
            # чтобы молча ничего не делать.
            logger.warning(
                "'%s' не найдено в закешированном списке устройств, обновляю список...", name
            )
            self._devices = get_devices()
            device = self._current_device_dict(name)

        if device is None:
            logger.error("'%s' не найдено даже после обновления списка устройств — переключение отменено.", name)
            return

        if self.on_device_selected is None:
            logger.error("on_device_selected не задан — переключение устройства не будет применено.")
            return

        self.config["device_name"] = name

        logger.info("Передаю устройство '%s' (id=%s) в on_device_selected", device["name"], device["id"])
        self.on_device_selected(device)
        auto_save(self.config)

    def _refresh_devices(self):
        current_name = self.device_var.get()

        self._devices = get_devices()
        names = [d["name"] for d in self._devices] or ["—"]
        self.device_menu.configure(values=names)

        if current_name in names:
            # устройство осталось доступным — НЕ трогаем выбор
            self.device_var.set(current_name)
        else:
            default_device = pick_default_from(self._devices)
            fallback_name = default_device["name"] if default_device else (names[0] if names else "")
            self.device_var.set(fallback_name)
            self._notify_device_selected(fallback_name)

    def _on_device_changed(self, value):
        if self._suppress_device_callback:
            return
        self._notify_device_selected(value)

    def set_selected_device_silently(self, name):
        """Откатывает выбор в выпадающем списке БЕЗ повторного вызова
        on_device_selected. Нужно для сценария: пользователь выбрал
        устройство, оно не открылось — откатываем UI на реально активное
        устройство, не пытаясь снова открыть уже упавшее (что могло бы
        зациклиться, если main.py вызовет этот метод из обработчика ошибки).
        """
        self._suppress_device_callback = True
        try:
            self.device_var.set(name)
        finally:
            self._suppress_device_callback = False

    def notify_engine_error(self, message):
        messagebox.showerror(self._tr("engine_error_title"), message)

    def _build_mode_section(self):
        frame = ctk.CTkFrame(self)
        frame.pack(fill="x", padx=10, pady=5)

        mode_frame = ctk.CTkFrame(frame, fg_color="transparent")
        mode_frame.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        mode_lbl = ctk.CTkLabel(mode_frame, text="")
        mode_lbl.pack(anchor="w")
        self._add_translatable(mode_lbl, "mode_label")

        btn_row = ctk.CTkFrame(mode_frame, fg_color="transparent")
        btn_row.pack(fill="x")

        self.mode_buttons = {}
        mode_keys = {MODE_VOLUME: "mode_volume", MODE_PITCH: "mode_pitch", MODE_COMBO: "mode_combo"}
        for mode, key in mode_keys.items():
            btn = ctk.CTkButton(btn_row, text="", command=lambda m=mode: self._select_mode(m))
            btn.pack(side="left", expand=True, fill="x", padx=2)
            self._add_translatable(btn, key)
            self.mode_buttons[mode] = btn

        idle_frame = ctk.CTkFrame(frame, fg_color="transparent")
        idle_frame.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        timeout_lbl = ctk.CTkLabel(idle_frame, text="")
        timeout_lbl.pack(anchor="w")
        self._add_translatable(timeout_lbl, "idle_timeout_label")

        self.idle_timeout_var = tk.StringVar(value=str(self.config.get("idle_timeout", IDLE_TIMEOUT_DEFAULT)))
        idle_entry = ctk.CTkEntry(idle_frame, textvariable=self.idle_timeout_var)
        idle_entry.pack(fill="x")
        idle_entry.bind("<FocusOut>", lambda e: self._apply_default_on_blur(
            self.idle_timeout_var, IDLE_TIMEOUT_DEFAULT
        ))

    def _build_shaking_section(self, parent):
        lbl = ctk.CTkLabel(parent, text="Shaking")
        lbl.pack(anchor="w", padx=5, pady=(5, 0))

        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.pack(fill="x", padx=5, pady=5)

        # Enable checkbox
        self.shaking_enabled_var = tk.BooleanVar(value=self.config["shaking"].get("enabled", False))
        shaking_check = ctk.CTkCheckBox(frame, text="Enable", variable=self.shaking_enabled_var, command=self._on_shaking_enabled_changed)
        shaking_check.pack(anchor="w")

        # Amplitude input
        amp_row = ctk.CTkFrame(frame, fg_color="transparent")
        amp_row.pack(fill="x", pady=(5, 0))
        amp_lbl = ctk.CTkLabel(amp_row, text="Amplitude (px):")
        amp_lbl.pack(side="left")
        self.shaking_amplitude_var = tk.StringVar(value=str(self.config["shaking"].get("amplitude", 10)))
        amp_entry = ctk.CTkEntry(amp_row, textvariable=self.shaking_amplitude_var, width=80)
        amp_entry.pack(side="right")
        amp_entry.bind("<FocusOut>", lambda e: self._on_shaking_amplitude_changed())

        # Speed input (called "Force" in UI)
        speed_row = ctk.CTkFrame(frame, fg_color="transparent")
        speed_row.pack(fill="x", pady=(5, 0))
        speed_lbl = ctk.CTkLabel(speed_row, text="Force:")
        speed_lbl.pack(side="left")
        self.shaking_speed_var = tk.StringVar(value=str(self.config["shaking"].get("speed", 5)))
        speed_entry = ctk.CTkEntry(speed_row, textvariable=self.shaking_speed_var, width=80)
        speed_entry.pack(side="right")
        speed_entry.bind("<FocusOut>", lambda e: self._on_shaking_speed_changed())

    def _on_shaking_enabled_changed(self):
        self.config["shaking"]["enabled"] = self.shaking_enabled_var.get()
        auto_save(self.config)

    def _on_shaking_amplitude_changed(self):
        try:
            value = int(self.shaking_amplitude_var.get())
            if 1 <= value <= 50:
                self.config["shaking"]["amplitude"] = value
                auto_save(self.config)
        except ValueError:
            pass

    def _on_shaking_speed_changed(self):
        try:
            value = int(self.shaking_speed_var.get())
            if 1 <= value <= 20:
                self.config["shaking"]["speed"] = value
                auto_save(self.config)
        except ValueError:
            pass

    def _apply_default_on_blur(self, var, default_value):
        """Если поле осталось пустым при потере фокуса — подставляет дефолт."""
        if var.get().strip() == "":
            var.set(str(default_value))
        try:
            self.config["idle_timeout"] = float(var.get())
            auto_save(self.config)
        except ValueError:
            pass

    def _build_scale_frames(self):
        container = ctk.CTkFrame(self)
        container.pack(fill="x", padx=10, pady=5)

        self.scale_canvases = {}
        self.experimental_warnings = {}

        self.scale_canvases[MODE_VOLUME] = StateScaleCanvas(
            container, "db", self.config["states"][MODE_VOLUME], self._on_state_selected, self._on_limit_reached
        )
        self.experimental_warnings[MODE_VOLUME] = None  # Нет предупреждения для режима громкости

        self.scale_canvases[MODE_PITCH] = StateScaleCanvas(
            container, "hz", self.config["states"][MODE_PITCH], self._on_state_selected, self._on_limit_reached
        )
        # Предупреждение для режима "Тон"
        pitch_warning = ctk.CTkLabel(
            container, text="", text_color="#FF6B6B", font=("Arial", 10)
        )
        self._add_translatable(pitch_warning, "experimental_warning")
        self.experimental_warnings[MODE_PITCH] = pitch_warning

        self.scale_canvases[MODE_COMBO] = StateScaleCanvas(
            container, "combo", self.config["states"][MODE_COMBO], self._on_state_selected, self._on_limit_reached
        )
        # Предупреждение для режима "Громкость + тон"
        combo_warning = ctk.CTkLabel(
            container, text="", text_color="#FF6B6B", font=("Arial", 10)
        )
        self._add_translatable(combo_warning, "experimental_warning")
        self.experimental_warnings[MODE_COMBO] = combo_warning

    def _on_limit_reached(self):
        messagebox.showwarning(self._tr("limit_title"), self._tr("limit_message"))

    def _select_mode(self, mode):
        self.current_mode = mode
        self.config["mode"] = mode
        auto_save(self.config)

        for m, canvas in self.scale_canvases.items():
            canvas.pack_forget()
        self.scale_canvases[mode].pack(fill="x", padx=5, pady=5)

        # Show/hide experimental mode warnings
        for m, warning in self.experimental_warnings.items():
            if warning is not None:
                warning.pack_forget()
        if self.experimental_warnings[mode] is not None:
            self.experimental_warnings[mode].pack(fill="x", padx=5, pady=(0, 5))

        for m, btn in self.mode_buttons.items():
            btn.configure(fg_color=("#3B8ED0" if m == mode else "#2b2b2b"))

        self._hide_detail_panel()

    # ---- единая область под графиком: либо настройки точки, либо idle ----

    def _build_point_and_idle_container(self):
        self.point_frame = ctk.CTkFrame(self)   # настройки выбранной точки (сверху, когда есть выбор)
        self.idle_frame = ctk.CTkFrame(self)    # настройки idle (снизу всегда; наверху, если ничего не выбрано)
        self.shaking_frame = ctk.CTkFrame(self)  # настройки шейкинга

        self._build_detail_panel(self.point_frame)
        self._build_idle_section(self.idle_frame)
        self._build_shaking_section(self.shaking_frame)

        self.idle_frame.pack(fill="x", padx=10, pady=5)
        self.shaking_frame.pack(fill="x", padx=10, pady=5)

    def _build_detail_panel(self, parent):
        name_lbl = ctk.CTkLabel(parent, text="")
        name_lbl.pack(anchor="w", padx=10, pady=(10, 0))
        self._add_translatable(name_lbl, "name_label")

        top_row = ctk.CTkFrame(parent, fg_color="transparent")
        top_row.pack(fill="x", padx=10, pady=(0, 5))

        self.detail_name_var = tk.StringVar()
        self.detail_name_var.trace_add("write", self._on_detail_name_changed)
        self.detail_name_entry = ctk.CTkEntry(top_row, textvariable=self.detail_name_var)
        self.detail_name_entry.pack(side="left", fill="x", expand=True)

        ctk.CTkButton(top_row, text="✕", width=30, command=self._hide_detail_panel).pack(side="left", padx=(5, 0))

        img_row = ctk.CTkFrame(parent, fg_color="transparent")
        img_row.pack(fill="x", padx=10, pady=5)

        self.detail_normal_btn = ctk.CTkButton(img_row, text="", command=lambda: self._pick_image("normal"))
        self.detail_normal_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.detail_blink_btn = ctk.CTkButton(img_row, text="", command=lambda: self._pick_image("blink"))
        self.detail_blink_btn.pack(side="left", fill="x", expand=True)

        self.blink_settings_frame = ctk.CTkFrame(parent, fg_color="transparent")

        interval_row = ctk.CTkFrame(self.blink_settings_frame, fg_color="transparent")
        interval_row.pack(fill="x", pady=2)
        interval_lbl = ctk.CTkLabel(interval_row, text="")
        interval_lbl.pack(side="left")
        self._add_translatable(interval_lbl, "interval_label")

        self.blink_min_var = tk.StringVar()
        blink_min_entry = ctk.CTkEntry(interval_row, textvariable=self.blink_min_var, width=60)
        blink_min_entry.pack(side="left", padx=5)
        blink_min_entry.bind("<FocusOut>", lambda e: self._apply_default_on_blur(
            self.blink_min_var, BLINK_INTERVAL_MIN_DEFAULT
        ))

        ctk.CTkLabel(interval_row, text="—").pack(side="left")

        self.blink_max_var = tk.StringVar()
        blink_max_entry = ctk.CTkEntry(interval_row, textvariable=self.blink_max_var, width=60)
        blink_max_entry.pack(side="left", padx=5)
        blink_max_entry.bind("<FocusOut>", lambda e: self._apply_default_on_blur(
            self.blink_max_var, BLINK_INTERVAL_MAX_DEFAULT
        ))

        duration_row = ctk.CTkFrame(self.blink_settings_frame, fg_color="transparent")
        duration_row.pack(fill="x", pady=2)
        duration_lbl = ctk.CTkLabel(duration_row, text="")
        duration_lbl.pack(side="left")
        self._add_translatable(duration_lbl, "duration_label")

        self.blink_duration_var = tk.StringVar()
        blink_dur_entry = ctk.CTkEntry(duration_row, textvariable=self.blink_duration_var, width=60)
        blink_dur_entry.pack(side="left", padx=5)
        blink_dur_entry.bind("<FocusOut>", lambda e: self._apply_default_on_blur(
            self.blink_duration_var, BLINK_DURATION_DEFAULT_MS
        ))

        for var in (self.blink_min_var, self.blink_max_var, self.blink_duration_var):
            var.trace_add("write", self._on_blink_settings_changed)

        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill="x", padx=10, pady=10)

        self.save_state_btn = ctk.CTkButton(
            btn_row, text="", command=self._save_current_state,
            fg_color=COLOR_SAVE_GREEN, hover_color=COLOR_SAVE_GREEN_HOVER,
        )
        self.save_state_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self._add_translatable(self.save_state_btn, "save_btn")

        delete_btn = ctk.CTkButton(
            btn_row, text="", fg_color="#a33", hover_color="#822", command=self._delete_current_state
        )
        delete_btn.pack(side="left", fill="x", expand=True)
        self._add_translatable(delete_btn, "delete_btn")

        self._current_editing_state = None
        self._state_dirty = False
        self._populating = False

    def _hide_detail_panel(self):
        canvas = self.scale_canvases[self.current_mode]
        canvas.deselect()
        self.point_frame.pack_forget()
        self.idle_frame.pack_forget()
        self.idle_frame.pack(fill="x", padx=10, pady=5)  # idle возвращается на место точки
        self._current_editing_state = None

    def _on_state_selected(self, state, is_draft=False, live_drag=False):
        self._current_editing_state = state
        if state is None:
            self.point_frame.pack_forget()
            self.idle_frame.pack_forget()
            self.idle_frame.pack(fill="x", padx=10, pady=5)
            return

        if not live_drag:
            self._populating = True
            self.detail_name_var.set(state.get("name", ""))
            self._refresh_image_buttons(state)
            self._refresh_blink_settings_ui(state)
            self._populating = False

            self.idle_frame.pack_forget()
            self.point_frame.pack(fill="x", padx=10, pady=5)
            self.idle_frame.pack(fill="x", padx=10, pady=5)  # idle съезжает под настройки точки

            self._state_dirty = False
        else:
            # перетаскивание точки меняет loud_db/pitch_hz - это тоже изменение
            self._state_dirty = True

        self._update_save_button_state(state)

    def _refresh_image_buttons(self, state):
        normal = state.get("normal")
        normal_label = self._tr("normal_label")
        not_set = self._tr("not_set")
        self.detail_normal_btn.configure(
            text=f"{normal_label}: {normal.split('/')[-1] if normal else not_set}"
        )
        blink = state.get("blink")
        blink_label = self._tr("blink_label")
        self.detail_blink_btn.configure(
            text=f"{blink_label}: {blink.split('/')[-1] if blink else not_set}",
            text_color=("white" if blink else COLOR_TEXT_MUTED),
        )
        if blink:
            self.blink_settings_frame.pack(fill="x", padx=10, pady=5)
            was_populating = self._populating
            self._populating = True
            self.blink_min_var.set(str(state.get("blink_interval_min", BLINK_INTERVAL_MIN_DEFAULT)))
            self.blink_max_var.set(str(state.get("blink_interval_max", BLINK_INTERVAL_MAX_DEFAULT)))
            self.blink_duration_var.set(str(state.get("blink_duration_ms", BLINK_DURATION_DEFAULT_MS)))
            self._populating = was_populating
        else:
            self.blink_settings_frame.pack_forget()

    def _refresh_image_buttons_text(self):
        if self._current_editing_state is not None:
            self._refresh_image_buttons(self._current_editing_state)

    def _refresh_blink_settings_ui(self, state):
        pass  # уже сделано в _refresh_image_buttons

    def _pick_image(self, kind):
        state = self._current_editing_state
        if state is None:
            return

        path = filedialog.askopenfilename(filetypes=[("PNG images", "*.png"), ("All files", "*.*")])
        if not path:
            return

        state[kind] = path
        if kind == "blink" and state.get("blink_interval_min") is None:
            state["blink_interval_min"] = BLINK_INTERVAL_MIN_DEFAULT
            state["blink_interval_max"] = BLINK_INTERVAL_MAX_DEFAULT
            state["blink_duration_ms"] = BLINK_DURATION_DEFAULT_MS

        self._state_dirty = True
        self._refresh_image_buttons(state)
        self._update_save_button_state(state)
        auto_save(self.config)

    def _on_detail_name_changed(self, *_args):
        if self._populating:
            return
        state = self._current_editing_state
        if state is not None:
            state["name"] = self.detail_name_var.get()
            self._state_dirty = True
            self._update_save_button_state(state)
            auto_save(self.config)

    def _on_blink_settings_changed(self, *_args):
        if self._populating:
            return
        state = self._current_editing_state
        if state is None or not state.get("blink"):
            return
        try:
            state["blink_interval_min"] = float(self.blink_min_var.get())
        except ValueError:
            pass
        try:
            state["blink_interval_max"] = float(self.blink_max_var.get())
        except ValueError:
            pass
        try:
            state["blink_duration_ms"] = int(self.blink_duration_var.get())
        except ValueError:
            pass
        auto_save(self.config)
        self._state_dirty = True
        self._update_save_button_state(state)

    def _update_save_button_state(self, state):
        complete = is_state_complete(state)
        enable = complete and self._state_dirty
        self.save_state_btn.configure(state="normal" if enable else "disabled")

    def _save_current_state(self):
        state = self._current_editing_state
        if state is None:
            return
        canvas = self.scale_canvases[self.current_mode]
        canvas.commit_draft()
        canvas.redraw()
        self._state_dirty = False
        self._update_save_button_state(state)
        auto_save(self.config)

    def _delete_current_state(self):
        state = self._current_editing_state
        if state is None:
            return
        canvas = self.scale_canvases[self.current_mode]
        canvas.remove_state(state)
        self._hide_detail_panel()
        auto_save(self.config)

    def _build_idle_section(self, parent):
        lbl = ctk.CTkLabel(parent, text="Idle")  # "idle" не переводится по спецификации
        lbl.pack(anchor="w", padx=5, pady=(5, 0))

        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=5, pady=5)

        self.idle_normal_btn = ctk.CTkButton(row, text="", command=lambda: self._pick_idle_image("normal"))
        self.idle_normal_btn.pack(side="left", fill="x", expand=True, padx=(0, 5))

        self.idle_blink_btn = ctk.CTkButton(row, text="", command=lambda: self._pick_idle_image("blink"))
        self.idle_blink_btn.pack(side="left", fill="x", expand=True)

        self.idle_blink_settings_frame = ctk.CTkFrame(parent, fg_color="transparent")

        idle = self.config["idle"]

        interval_row = ctk.CTkFrame(self.idle_blink_settings_frame, fg_color="transparent")
        interval_row.pack(fill="x", padx=5, pady=2)
        idle_interval_lbl = ctk.CTkLabel(interval_row, text="")
        idle_interval_lbl.pack(side="left")
        self._add_translatable(idle_interval_lbl, "interval_label")

        self.idle_blink_min_var = tk.StringVar(value=str(idle.get("blink_interval_min", BLINK_INTERVAL_MIN_DEFAULT)))
        idle_min_entry = ctk.CTkEntry(interval_row, textvariable=self.idle_blink_min_var, width=60)
        idle_min_entry.pack(side="left", padx=5)
        idle_min_entry.bind("<FocusOut>", lambda e: self._apply_default_on_blur(
            self.idle_blink_min_var, BLINK_INTERVAL_MIN_DEFAULT
        ))

        ctk.CTkLabel(interval_row, text="—").pack(side="left")

        self.idle_blink_max_var = tk.StringVar(value=str(idle.get("blink_interval_max", BLINK_INTERVAL_MAX_DEFAULT)))
        idle_max_entry = ctk.CTkEntry(interval_row, textvariable=self.idle_blink_max_var, width=60)
        idle_max_entry.pack(side="left", padx=5)
        idle_max_entry.bind("<FocusOut>", lambda e: self._apply_default_on_blur(
            self.idle_blink_max_var, BLINK_INTERVAL_MAX_DEFAULT
        ))

        duration_row = ctk.CTkFrame(self.idle_blink_settings_frame, fg_color="transparent")
        duration_row.pack(fill="x", padx=5, pady=2)
        idle_duration_lbl = ctk.CTkLabel(duration_row, text="")
        idle_duration_lbl.pack(side="left")
        self._add_translatable(idle_duration_lbl, "duration_label")

        self.idle_blink_duration_var = tk.StringVar(
            value=str(idle.get("blink_duration_ms", BLINK_DURATION_DEFAULT_MS))
        )
        idle_dur_entry = ctk.CTkEntry(duration_row, textvariable=self.idle_blink_duration_var, width=60)
        idle_dur_entry.pack(side="left", padx=5)
        idle_dur_entry.bind("<FocusOut>", lambda e: self._apply_default_on_blur(
            self.idle_blink_duration_var, BLINK_DURATION_DEFAULT_MS
        ))

        for var in (self.idle_blink_min_var, self.idle_blink_max_var, self.idle_blink_duration_var):
            var.trace_add("write", self._on_idle_blink_settings_changed)

        self._refresh_idle_buttons()

    def _pick_idle_image(self, kind):
        path = filedialog.askopenfilename(filetypes=[("PNG images", "*.png"), ("All files", "*.*")])
        if not path:
            return

        idle = self.config["idle"]
        idle[kind] = path
        if kind == "blink":
            idle.setdefault("blink_interval_min", BLINK_INTERVAL_MIN_DEFAULT)
            idle.setdefault("blink_interval_max", BLINK_INTERVAL_MAX_DEFAULT)
            idle.setdefault("blink_duration_ms", BLINK_DURATION_DEFAULT_MS)

        self._refresh_idle_buttons()
        auto_save(self.config)

    def _refresh_idle_buttons(self):
        idle = self.config["idle"]
        normal_label = self._tr("normal_label")
        blink_label = self._tr("blink_label")
        not_set = self._tr("not_set")

        normal = idle.get("normal")
        self.idle_normal_btn.configure(text=f"{normal_label}: {normal.split('/')[-1] if normal else not_set}")

        blink = idle.get("blink")
        self.idle_blink_btn.configure(
            text=f"{blink_label}: {blink.split('/')[-1] if blink else not_set}",
            text_color=("white" if blink else COLOR_TEXT_MUTED),
        )

        if blink:
            self.idle_blink_settings_frame.pack(fill="x", padx=5, pady=5)
        else:
            self.idle_blink_settings_frame.pack_forget()

    def _on_idle_blink_settings_changed(self, *_args):
        idle = self.config["idle"]
        if not idle.get("blink"):
            return
        try:
            idle["blink_interval_min"] = float(self.idle_blink_min_var.get())
        except ValueError:
            pass
        try:
            idle["blink_interval_max"] = float(self.idle_blink_max_var.get())
        except ValueError:
            pass
        try:
            idle["blink_duration_ms"] = int(self.idle_blink_duration_var.get())
        except ValueError:
            pass
        auto_save(self.config)

    def _save_all(self):
        self.config["device_name"] = self.device_var.get()

        try:
            self.config["idle_timeout"] = float(self.idle_timeout_var.get())
        except ValueError:
            pass

        auto_save(self.config)

    # ---- живой сигнал ----

    def _tick(self):
        rms, pitch = self.get_live()
        live_db = rms_to_db(rms)
        self.scale_canvases[self.current_mode].set_live(live_db, pitch)
        self.after(50, self._tick)
