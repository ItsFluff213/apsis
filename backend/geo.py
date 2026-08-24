"""Geodesy helpers for landing/return guidance on a (roughly spherical)
celestial body."""

import math

# Approximate location of the KSC launch pad / runway area on Kerbin.
KSC_PAD_LATITUDE = -0.0972
KSC_PAD_LONGITUDE = -74.5577


def surface_distance(body, lat1, lon1, lat2, lon2):
    """Great-circle distance in meters between two lat/lon points on body."""
    r = body.equatorial_radius
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(a)))


def bearing_to(lat1, lon1, lat2, lon2):
    """Compass bearing in degrees (0 = north) from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360
