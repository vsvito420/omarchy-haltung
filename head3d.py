"""Low-poly 3D head for the haltung panel, flat-shaded with cairo.

Head coordinates: x towards the right ear, y up, z out of the nose. The head
rotates about a pivot at the top of the neck; neck and shoulders stay put, so
the tilt reads against something fixed.
"""
import math

VIEW_YAW = math.radians(-28)    # three-quarter view: a nod and a sideways tilt are both visible
VIEW_PITCH = math.radians(10)
LIGHT = (-0.45, 0.55, 0.70)
PIVOT = (0.0, -0.95, -0.05)


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return tuple(x / n for x in v)


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _ellipsoid(center, radii, lat, lon, shape=None, tag="skin"):
    """Faces of a UV ellipsoid, wound outward. shape(x, y, z) may reshape each vertex."""
    cx, cy, cz = center
    rx, ry, rz = radii
    rings = []
    for i in range(lat + 1):
        th = math.pi * i / lat
        ring = []
        for j in range(lon):
            ph = 2 * math.pi * j / lon
            x, y, z = math.sin(th) * math.cos(ph), math.cos(th), math.sin(th) * math.sin(ph)
            if shape:
                x, y, z = shape(x, y, z)
            ring.append((cx + rx * x, cy + ry * y, cz + rz * z))
        rings.append(ring)
    faces = []
    for i in range(lat):
        for j in range(lon):
            k = (j + 1) % lon
            quad = [rings[i][j], rings[i][k], rings[i + 1][k], rings[i + 1][j]]
            faces.append(_orient(quad, center, tag))
    return faces


def _orient(poly, center, tag):
    # Drop the duplicated pole vertex so the normal stays defined.
    pts = [p for i, p in enumerate(poly) if p != poly[i - 1]]
    if len(pts) < 3:
        pts = poly[:3]
    n = _cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0]))
    mid = tuple(sum(p[i] for p in pts) / len(pts) for i in range(3))
    if _dot(n, _sub(mid, center)) < 0:
        pts = pts[::-1]
    return (pts, tag)


def _skull(x, y, z):
    # Narrower jaw and chin, slightly fuller back of the head.
    if y < 0:
        k = 1 - 0.28 * (-y) ** 1.5
        x, z = x * k, z * k
        if z > 0:
            z *= 1 + 0.12 * -y
    if z < 0:
        z *= 1.08
    return x, y, z


def _build():
    head = _ellipsoid((0, 0, 0), (0.76, 0.98, 0.90), 9, 14, _skull)
    # nose: a small wedge on the face
    tip, top = (0, -0.18, 1.08), (0, 0.12, 0.86)
    left, right, low = (-0.13, -0.24, 0.84), (0.13, -0.24, 0.84), (0, -0.30, 0.86)
    nose_c = (0, -0.12, 0.80)
    head += [_orient(f, nose_c, "skin") for f in (
        [top, right, tip], [top, tip, left], [right, low, tip], [low, left, tip])]
    # eyes and ears
    for sx in (-1, 1):
        head += _ellipsoid((0.27 * sx, 0.14, 0.80), (0.10, 0.06, 0.05), 3, 6, tag="eye")
        head += _ellipsoid((0.76 * sx, 0.0, -0.06), (0.10, 0.26, 0.16), 3, 6, tag="ear")
    # AirPods in the ears, so it is clear what is measuring
    for sx in (-1, 1):
        head += _ellipsoid((0.86 * sx, -0.06, 0.02), (0.07, 0.07, 0.07), 3, 6, tag="pod")
        head += _ellipsoid((0.88 * sx, -0.26, 0.05), (0.035, 0.17, 0.035), 2, 5, tag="pod")
    body = _ellipsoid((0, -1.25, -0.05), (0.33, 0.42, 0.32), 4, 10, tag="body")
    body += _ellipsoid((0, -1.95, -0.10), (1.25, 0.42, 0.62), 4, 14, tag="body")
    return head, body


HEAD_FACES, BODY_FACES = _build()


def _rot_x(p, a):
    c, s = math.cos(a), math.sin(a)
    return (p[0], p[1] * c - p[2] * s, p[1] * s + p[2] * c)


def _rot_y(p, a):
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c + p[2] * s, p[1], -p[0] * s + p[2] * c)


def _rot_z(p, a):
    c, s = math.cos(a), math.sin(a)
    return (p[0] * c - p[1] * s, p[0] * s + p[1] * c, p[2])


def draw(cr, w, h, pitch_deg, roll_deg, colors):
    """pitch > 0 tips the nose down, roll > 0 tilts towards the right ear."""
    pitch, roll = math.radians(pitch_deg), math.radians(roll_deg)
    scale = min(w / 2.9, h / 3.3)
    ox, oy = w / 2, h * 0.36
    light = _unit(LIGHT)

    def head_xf(p):
        q = _sub(p, PIVOT)
        q = _rot_z(q, -roll)
        q = _rot_x(q, -pitch)   # the nose (+z) swings towards -y
        return (q[0] + PIVOT[0], q[1] + PIVOT[1], q[2] + PIVOT[2])

    def view(p):
        return _rot_x(_rot_y(p, VIEW_YAW), VIEW_PITCH)

    polys = []
    for faces, xf in ((HEAD_FACES, head_xf), (BODY_FACES, None)):
        for pts, tag in faces:
            vp = [view(xf(p) if xf else p) for p in pts]
            n = _cross(_sub(vp[1], vp[0]), _sub(vp[2], vp[0]))
            if n[2] <= 0:          # facing away from the camera
                continue
            depth = sum(p[2] for p in vp) / len(vp)
            polys.append((depth, vp, tag, _dot(_unit(n), light)))
    polys.sort(key=lambda t: t[0])

    for _, vp, tag, lum in polys:
        base = colors[tag]
        k = 0.30 + 0.70 * max(0.0, lum)
        cr.set_source_rgb(*(min(1.0, c * k) for c in base))
        cr.move_to(ox + vp[0][0] * scale, oy - vp[0][1] * scale)
        for p in vp[1:]:
            cr.line_to(ox + p[0] * scale, oy - p[1] * scale)
        cr.close_path()
        cr.fill_preserve()
        cr.set_line_width(0.8)     # hides the hairline seams between flat faces
        cr.stroke()
