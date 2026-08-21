DB_MIN, DB_MAX = -60.0, 0.0
HZ_MIN, HZ_MAX = 50.0, 500.0

DB_MINOR_STEP, DB_MAJOR_STEP = 2.0, 20.0
HZ_MINOR_STEP, HZ_MAJOR_STEP = 10.0, 50.0


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def db_to_x(db, track_min_px, track_max_px):
    ratio = (clamp(db, DB_MIN, DB_MAX) - DB_MIN) / (DB_MAX - DB_MIN)
    return track_min_px + ratio * (track_max_px - track_min_px)


def x_to_db(x, track_min_px, track_max_px):
    ratio = (clamp(x, track_min_px, track_max_px) - track_min_px) / (
        track_max_px - track_min_px
    )
    return DB_MIN + ratio * (DB_MAX - DB_MIN)


def hz_to_y(hz, track_min_px, track_max_px):
    """Hz vertically — higher value is higher (lower y in pixels)."""
    ratio = (clamp(hz, HZ_MIN, HZ_MAX) - HZ_MIN) / (HZ_MAX - HZ_MIN)
    return track_max_px - ratio * (track_max_px - track_min_px)


def y_to_hz(y, track_min_px, track_max_px):
    ratio = (track_max_px - clamp(y, track_min_px, track_max_px)) / (
        track_max_px - track_min_px
    )
    return HZ_MIN + ratio * (HZ_MAX - HZ_MIN)


def hz_to_x(hz, track_min_px, track_max_px):
    """For 1D pitch scale (horizontal axis, not vertical like in combo)."""
    ratio = (clamp(hz, HZ_MIN, HZ_MAX) - HZ_MIN) / (HZ_MAX - HZ_MIN)
    return track_min_px + ratio * (track_max_px - track_min_px)


def x_to_hz(x, track_min_px, track_max_px):
    ratio = (clamp(x, track_min_px, track_max_px) - track_min_px) / (
        track_max_px - track_min_px
    )
    return HZ_MIN + ratio * (HZ_MAX - HZ_MIN)


def ticks(minor_step, major_step, value_min, value_max):
    """Returns list of (value, is_major) for drawing scale ticks."""
    result = []
    value = value_min
    while value <= value_max + 1e-9:
        is_major = abs((value - value_min) % major_step) < 1e-6
        result.append((round(value, 2), is_major))
        value += minor_step
    return result
