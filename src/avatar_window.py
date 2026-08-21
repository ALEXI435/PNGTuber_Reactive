import math
import os
import random
import time
import tkinter as tk

from PIL import Image, ImageDraw, ImageFont, ImageTk
from config import auto_save

CHROMA_KEY_COLOR = "#00FF00"

ZOOM_STEP = 1.1
ZOOM_MIN = 0.1
ZOOM_MAX = 5.0


def _make_media_offline_image(width, height):
    """Generates DaVinci Resolve style 'Media Offline' placeholder —
    checkerboard pattern + label. Nothing stored on disk, image is
    drawn programmatically via Pillow each time needed.
    """
    width = max(width, 1)
    height = max(height, 1)
    img = Image.new("RGBA", (width, height), (40, 40, 40, 255))
    draw = ImageDraw.Draw(img)

    cell = 20
    for y in range(0, height, cell):
        for x in range(0, width, cell):
            if (x // cell + y // cell) % 2 == 0:
                draw.rectangle([x, y, x + cell, y + cell], fill=(60, 20, 30, 255))

    text = "MEDIA OFFLINE"
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.rectangle(
        [
            width // 2 - text_w // 2 - 10,
            height // 2 - text_h // 2 - 6,
            width // 2 + text_w // 2 + 10,
            height // 2 + text_h // 2 + 6,
        ],
        fill=(0, 0, 0, 255),
    )
    draw.text(
        (width // 2 - text_w // 2, height // 2 - text_h // 2),
        text,
        fill=(255, 255, 255, 255),
        font=font,
    )
    return img


class AvatarWindow:
    """Separate window with fixed green background (chroma key for OBS Window
    Capture). Shows normal/blink image of current active state
    (or idle if no state selected) in its natural size (with zoom),
    without stretching to window size. If window is smaller than image —
    it's simply cropped at window edges (canvas does this automatically).
    If image path is broken — shows Media Offline instead of crash.

    Zoom (mouse wheel) and pan (LMB + drag) are global parameters,
    applied equally to all images and saved in config.
    """

    def __init__(self, config, master=None):
        self.config = config

        win_cfg = config["avatar_window"]
        view_cfg = config["avatar_view"]

        self.root = tk.Toplevel(master) if master is not None else tk.Tk()
        self.root.title("PNGTube Avatar")
        self.root.configure(bg=CHROMA_KEY_COLOR)
        self.root.geometry(
            f"{win_cfg['width']}x{win_cfg['height']}+{win_cfg['x']}+{win_cfg['y']}"
        )

        self.canvas = tk.Canvas(
            self.root, bg=CHROMA_KEY_COLOR, highlightthickness=0
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.zoom = view_cfg.get("zoom", 1.0)
        self.pan_x = view_cfg.get("pan_x", 0)
        self.pan_y = view_cfg.get("pan_y", 0)

        self._drag_start = None  # (event.x, event.y, pan_x0, pan_y0)

        self._current_tk_image = None
        self._raw_image_cache = {}  # path -> (mtime, PIL.Image RGBA)
        self._resized_cache = {}  # (path, zoom) -> PIL.Image (optimized rendering)

        self._blink_state = None
        self._current_state_key = None

        self._closed = False
        self._last_source = None
        self._current_image_id = None  # Canvas image ID for updating instead of recreate

        # Shaking animation
        self._shaking_start_time = None

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._on_configure)

        self.canvas.bind("<MouseWheel>", self._on_wheel)       # Windows
        self.canvas.bind("<Button-4>", self._on_wheel_linux_up)   # Linux (доп. поддержка)
        self.canvas.bind("<Button-5>", self._on_wheel_linux_down)

        self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self.canvas.bind("<B1-Motion>", self._on_pan_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_pan_end)

    # ---- window ----

    def _on_configure(self, event):
        if event.widget is self.root:
            self.config["avatar_window"]["x"] = self.root.winfo_x()
            self.config["avatar_window"]["y"] = self.root.winfo_y()
            self.config["avatar_window"]["width"] = self.root.winfo_width()
            self.config["avatar_window"]["height"] = self.root.winfo_height()
            auto_save(self.config)

    def _on_close(self):
        self._closed = True
        self.root.destroy()

    def is_closed(self):
        return self._closed

    # ---- zoom/pan ----

    def _set_zoom(self, new_zoom):
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, new_zoom))
        self.config["avatar_view"]["zoom"] = self.zoom
        auto_save(self.config)

    def _on_wheel(self, event):
        # Windows: delta > 0 -> wheel "forward" (away) -> decrease
        #          delta < 0 -> wheel "backward" (toward) -> increase
        if event.delta > 0:
            self._set_zoom(self.zoom / ZOOM_STEP)
        else:
            self._set_zoom(self.zoom * ZOOM_STEP)
        self._redraw_current()

    def _on_wheel_linux_up(self, _event):
        self._set_zoom(self.zoom / ZOOM_STEP)
        self._redraw_current()

    def _on_wheel_linux_down(self, _event):
        self._set_zoom(self.zoom * ZOOM_STEP)
        self._redraw_current()

    def _on_pan_start(self, event):
        self._drag_start = (event.x, event.y, self.pan_x, self.pan_y)

    def _on_pan_drag(self, event):
        if self._drag_start is None:
            return
        sx, sy, pan_x0, pan_y0 = self._drag_start
        self.pan_x = pan_x0 + (event.x - sx)
        self.pan_y = pan_y0 + (event.y - sy)
        self.config["avatar_view"]["pan_x"] = self.pan_x
        self.config["avatar_view"]["pan_y"] = self.pan_y
        self._redraw_current()
        auto_save(self.config)

    def _on_pan_end(self, _event):
        self._drag_start = None

    # ---- images ----

    def _get_raw_image(self, path):
        """Loads and caches original (unstretched) image by path.
        If path is broken/file missing — Media Offline of same size
        as last valid image (or 300x300 if none yet).
        """
        if path:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = None
        else:
            mtime = None

        cache_key = path
        cached = self._raw_image_cache.get(cache_key)
        if cached is not None and cached[0] == mtime and mtime is not None:
            return cached[1]

        if path and mtime is not None:
            try:
                img = Image.open(path).convert("RGBA")
            except Exception:
                img = _make_media_offline_image(300, 300)
        else:
            img = _make_media_offline_image(300, 300)

        self._raw_image_cache[cache_key] = (mtime, img)
        return img

    def _get_resized_image(self, raw, zoom):
        """Gets cached resized image for given zoom level, or creates new one."""
        cache_key = (id(raw), zoom)  # Use id of raw image as key
        cached = self._resized_cache.get(cache_key)
        if cached is not None:
            return cached

        w = max(1, int(raw.width * zoom))
        h = max(1, int(raw.height * zoom))
        resized = raw.resize((w, h), Image.LANCZOS)
        self._resized_cache[cache_key] = resized
        return resized

    def _should_blink_now(self, state):
        """Controls individual blink timer for active state.
        If state has no blink specified — no blinking happens.
        """
        if not state or not state.get("blink"):
            self._blink_state = None
            return False

        state_key = state.get("id") or state.get("name")
        now = time.monotonic()

        if self._current_state_key != state_key or self._blink_state is None:
            self._current_state_key = state_key
            interval_min = state.get("blink_interval_min", 2.0)
            interval_max = state.get("blink_interval_max", 6.0)

            self._blink_state = {
                "next_blink_at": now + random.uniform(interval_min, interval_max),
                "blinking_until": 0.0,
            }

        if now < self._blink_state["blinking_until"]:
            return True

        if now >= self._blink_state["next_blink_at"]:
            duration_ms = state.get("blink_duration_ms", 150)
            interval_min = state.get("blink_interval_min", 2.0)
            interval_max = state.get("blink_interval_max", 6.0)

            self._blink_state["blinking_until"] = now + duration_ms / 1000.0
            self._blink_state["next_blink_at"] = (
                now + duration_ms / 1000.0 + random.uniform(interval_min, interval_max)
            )
            return True

        return False

    def render(self, state):
        """Renders current state (or idle config if state=None).
        Call on every tick from main loop.
        """
        if self._closed:
            return

        source = state if state is not None else self.config["idle"]
        self._last_source = source
        self._redraw_current()

    def _redraw_current(self):
        if self._closed or self._last_source is None:
            return

        source = self._last_source
        show_blink = self._should_blink_now(source)
        path = source.get("blink") if show_blink else source.get("normal")

        raw = self._get_raw_image(path)
        resized = self._get_resized_image(raw, self.zoom)

        tk_img = ImageTk.PhotoImage(resized)
        self._current_tk_image = tk_img  # keep reference, otherwise tkinter will garbage collect

        canvas_w = max(self.canvas.winfo_width(), 1)
        canvas_h = max(self.canvas.winfo_height(), 1)

        # Apply shaking offset (only for active states, not idle)
        shaking_cfg = self.config.get("shaking", {})
        is_idle = (source is self.config["idle"])
        if shaking_cfg.get("enabled", False) and not is_idle:
            if self._shaking_start_time is None:
                self._shaking_start_time = time.monotonic()

            elapsed = time.monotonic() - self._shaking_start_time
            amplitude = shaking_cfg.get("amplitude", 10)
            speed = shaking_cfg.get("speed", 5)

            # Circular motion (infinity/eight)
            offset_x = amplitude * math.sin(elapsed * speed)
            offset_y = amplitude * math.cos(elapsed * speed)
        else:
            self._shaking_start_time = None
            offset_x = 0
            offset_y = 0

        cx = canvas_w // 2 + self.pan_x + offset_x
        cy = canvas_h // 2 + self.pan_y + offset_y

        # Update existing image instead of recreate to avoid freezes
        if self._current_image_id is None:
            self.canvas.delete("all")
            self._current_image_id = self.canvas.create_image(cx, cy, image=tk_img, anchor="center")
        else:
            self.canvas.coords(self._current_image_id, cx, cy)
            self.canvas.itemconfig(self._current_image_id, image=tk_img)

    def update_idle(self):
        """Updates tkinter UI loop without blocking (call instead of mainloop
        when testing independently without common master loop)."""
        if not self._closed:
            self.root.update_idletasks()
            self.root.update()
