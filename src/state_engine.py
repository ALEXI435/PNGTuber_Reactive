import math
import time

DB_FLOOR = -60.0  # volume floor — below this considered "complete silence"


def rms_to_db(rms):
    """Converts linear RMS (0..1) to dB. Silence/0 -> DB_FLOOR (not -inf)."""
    if rms <= 0:
        return DB_FLOOR
    db = 20.0 * math.log10(rms)
    return max(db, DB_FLOOR)


def state_matches(state, live_db, live_pitch):
    """State matches if ALL specified thresholds are met (AND).

    State can have only loud_db (volume mode), only pitch_hz (pitch mode),
    or both (volume + pitch mode). Threshold equal to None is not checked.
    """
    loud_db = state.get("loud_db")
    pitch_hz = state.get("pitch_hz")

    if loud_db is not None and not (live_db > loud_db):
        return False
    if pitch_hz is not None and not (live_pitch > pitch_hz):
        return False

    return True


def _distance_to_corner(state, live_db, live_pitch):
    """Distance from live point to state threshold corner, counting
    only axes specified for state (one axis in 1D modes).
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
    """Returns state (dict) from states matching (live_db, live_pitch)
    right now, or None if none matched.

    If multiple matched — closest by distance to threshold corner wins.
    On equal distance — higher volume threshold wins.
    """
    matches = [s for s in states if state_matches(s, live_db, live_pitch)]

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    def sort_key(s):
        distance = _distance_to_corner(s, live_db, live_pitch)
        loud_db = s.get("loud_db")
        # Higher volume threshold should come first on equal distance
        # — so we take with minus (ascending sort).
        tie_break = -(loud_db if loud_db is not None else float("-inf"))
        return (distance, tie_break)

    matches.sort(key=sort_key)
    return matches[0]


class StateSelector:
    """Holds "what's currently active" state between update() calls.

    Transition between two active states is instant. If no state matched —
    old active state is held for idle_timeout seconds (in case of short
    speech pause), and only if silence lasts longer — returns None (idle).
    If during wait something matches again (even different state) — timer
    resets and new match is returned immediately.

    No signal smoothing (EMA) — raw rms/pitch are input.
    """

    def __init__(self, idle_timeout):
        self.idle_timeout = idle_timeout
        self._last_active_state = None
        self._silence_started_at = None

    def update(self, states, rms, pitch, now=None):
        """Call on every tick (e.g. every 30-50 ms from GUI).

        rms — raw value from AudioEngine.get_latest() (linear, 0..1).
        pitch — raw value in Hz (0.0 if silence/undefined).

        Returns selected state (dict) or None if idle should be shown.
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
