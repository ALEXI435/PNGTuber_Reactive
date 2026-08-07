import math
import time

DB_FLOOR = -60.0  # пол по громкости — ниже этого считаем "полная тишина"


def rms_to_db(rms):
    """Переводит линейный RMS (0..1) в дБ. Тишина/0 -> DB_FLOOR (не -inf)."""
    if rms <= 0:
        return DB_FLOOR
    db = 20.0 * math.log10(rms)
    return max(db, DB_FLOOR)


def state_matches(state, live_db, live_pitch):
    """Состояние подходит, если выполнены ВСЕ заданные для него пороги (И).

    Состояние может иметь только loud_db (режим "громкость"), только
    pitch_hz (режим "тон"), или оба сразу (режим "громкость + тон").
    Порог, равный None, в проверке не участвует.
    """
    loud_db = state.get("loud_db")
    pitch_hz = state.get("pitch_hz")

    if loud_db is not None and not (live_db > loud_db):
        return False
    if pitch_hz is not None and not (live_pitch > pitch_hz):
        return False

    return True


def _distance_to_corner(state, live_db, live_pitch):
    """Расстояние от живой точки до порогового угла состояния, считая
    только те оси, что заданы у состояния (в 1D-режимах — одна ось).
    """
    squared = 0.0

    loud_db = state.get("loud_db")
    if loud_db is not None:
        squared += (live_db - loud_db) ** 2

    pitch_hz = state.get("pitch_hz")
    if pitch_hz is not None:
        squared += (live_pitch - pitch_hz) ** 2

    return math.sqrt(squared)


def select_state(states, live_db, live_pitch):
    """Возвращает состояние (dict) из states, подходящее для (live_db,
    live_pitch) прямо сейчас, или None, если ни одно не подошло.

    Если подошло несколько — побеждает ближайшее по расстоянию до порогового
    угла. При равном расстоянии — у кого выше порог громкости.
    """
    matches = [s for s in states if state_matches(s, live_db, live_pitch)]

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    def sort_key(s):
        distance = _distance_to_corner(s, live_db, live_pitch)
        loud_db = s.get("loud_db")
        # Чем выше порог громкости, тем раньше должен идти при равном
        # расстоянии — поэтому берём с минусом (сортировка по возрастанию).
        tie_break = -(loud_db if loud_db is not None else float("-inf"))
        return (distance, tie_break)

    matches.sort(key=sort_key)
    return matches[0]


class StateSelector:
    """Держит состояние "что сейчас активно" между вызовами update().

    Между двумя активными состояниями переход мгновенный. Если ни одно
    состояние не подошло — старое активное состояние удерживается ещё
    idle_timeout секунд (на случай если это просто короткая пауза в речи),
    и только если тишина длится дольше — возвращается None (idle). Если во
    время ожидания что-то снова подошло (даже другое состояние) — таймер
    сбрасывается и сразу возвращается новое совпадение.

    Никакого сглаживания (EMA) сигнала — на вход подаются сырые rms/pitch.
    """

    def __init__(self, idle_timeout):
        self.idle_timeout = idle_timeout
        self._last_active_state = None
        self._silence_started_at = None

    def update(self, states, rms, pitch, now=None):
        """Вызывать на каждый тик (например каждые 30-50 мс из GUI).

        rms — сырое значение из AudioEngine.get_latest() (линейное, 0..1).
        pitch — сырое значение в Гц (0.0, если тишина/не определено).

        Возвращает выбранное состояние (dict) или None, если сейчас должен
        показываться idle.
        """
        if now is None:
            now = time.monotonic()

        live_db = rms_to_db(rms)
        matched = select_state(states, live_db, pitch)

        if matched is not None:
            self._last_active_state = matched
            self._silence_started_at = None
            return matched

        if self._last_active_state is None:
            return None

        if self._silence_started_at is None:
            self._silence_started_at = now

        elapsed = now - self._silence_started_at
        if elapsed < self.idle_timeout:
            return self._last_active_state

        self._last_active_state = None
        self._silence_started_at = None
        return None
