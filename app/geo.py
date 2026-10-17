from math import asin, cos, radians, sin, sqrt


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = radians(lat1), radians(lat2)
    a = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371000 * asin(sqrt(a))


def nearest_site(lat: float, lng: float, accuracy: float, sites: list[dict]) -> tuple[dict | None, float | None, bool]:
    """Returns (nearest site, distance in m, inside). GPS error is forgiven up to 100 m."""
    best, best_d = None, None
    for s in sites:
        d = distance_m(lat, lng, s["lat"], s["lng"])
        if best_d is None or d < best_d:
            best, best_d = s, d
    if best is None:
        return None, None, True
    inside = best_d - min(max(accuracy, 0), 100) <= best["radius_m"]
    return best, round(best_d, 1), inside
