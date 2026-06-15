"""GPU (moderngl) offscreen render of the mount + scope + camera for a pointing.

Rendered server-side; the web client only displays the returned PNG. Run as a CLI
(``python -m asiairbridge.mount_render --ra .. --dec .. --out file.png``) so the GL
context lives in an isolated subprocess, never in the threaded web server.

The detailed equipment meshes (build_mount / build_scope / build_camera) are filled
in from the parallel modelling agents; this module owns the primitives, renderer,
kinematics and CLI.
"""
from __future__ import annotations

import argparse
import io
import math
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import numpy as np


# --------------------------------------------------------------------------- #
#  server-side helper: render in an isolated subprocess (GL context safety),  #
#  with a small cache keyed by the rounded pointing                           #
# --------------------------------------------------------------------------- #
_RENDER_CACHE: dict = {}
_RENDER_LOCK = threading.Lock()


def render_cached(params: dict, root: str, max_entries: int = 32) -> bytes:
    def f(name, default=0.0):
        v = params.get(name)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    from datetime import datetime as _dt
    key = (round(f("ra"), 3), round(f("dec", 90.0), 2), round(f("lst"), 3),
           str(params.get("pier")), round(f("lat", 40.0), 2), int(f("size", 560)),
           round(f("az", -999.0), 1), round(f("el", -999.0), 1), round(f("ha", -999.0), 1),
           int(f("sky", 0)), int(f("eqgrid", 1)), int(f("altgrid", 0)),
           round(f("tra", -999.0), 2), round(f("tdec", -999.0), 1), round(f("fov", 35.0), 0), int(f("ground", 1)),
           _dt.now().strftime("%Y%m%d%H") if f("sky", 0) else "")
    with _RENDER_LOCK:
        hit = _RENDER_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        png = _worker_render(params, root)
    except Exception:  # noqa: BLE001 — fall back to the one-shot subprocess
        png = _render_subprocess(params, root)
    with _RENDER_LOCK:
        _RENDER_CACHE[key] = png
        while len(_RENDER_CACHE) > max_entries:
            _RENDER_CACHE.pop(next(iter(_RENDER_CACHE)))
    return png


def _render_subprocess(params: dict, root: str) -> bytes:
    args = [sys.executable, "-B", "-m", "asiairbridge.mount_render"]
    for k in ("ra", "dec", "lst", "lat", "pier", "size", "az", "el", "ha", "sky", "eqgrid", "altgrid", "tra", "tdec", "fov", "ground"):
        v = params.get(k)
        if v not in (None, ""):
            args += [f"--{k}", str(v)]
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        args += ["--out", tmp]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(root) / "src")
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(args, cwd=str(root), env=env, capture_output=True, text=True, timeout=40, check=False)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            last_line = detail.splitlines()[-1] if detail else ""
            raise RuntimeError(last_line or f"mount render subprocess exited {proc.returncode}")
        with open(tmp, "rb") as fh:
            data = fh.read()
        if not data:
            raise RuntimeError("mount render subprocess produced an empty image")
        return data
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
#  mesh primitives (shared with the modelling agents)                         #
# --------------------------------------------------------------------------- #
def _arr(p):
    return np.asarray(p, dtype=np.float32)


def cylinder(r0, r1, h, seg=48, cap0=True, cap1=True):
    ang = np.linspace(0, 2 * math.pi, seg + 1)
    P = []
    N = []
    for i in range(seg):
        a0, a1 = ang[i], ang[i + 1]
        c0, s0 = math.cos(a0), math.sin(a0)
        c1, s1 = math.cos(a1), math.sin(a1)
        b0 = (r0 * c0, r0 * s0, 0.0)
        b1 = (r0 * c1, r0 * s1, 0.0)
        t1 = (r1 * c1, r1 * s1, h)
        t0 = (r1 * c0, r1 * s0, h)
        n0 = (c0, s0, 0.0)
        n1 = (c1, s1, 0.0)
        P += [b0, b1, t1, b0, t1, t0]
        N += [n0, n1, n1, n0, n1, n0]
    if cap1 and abs(r1) > 1e-6:
        for i in range(seg):
            a0, a1 = ang[i], ang[i + 1]
            P += [(0, 0, h), (r1 * math.cos(a0), r1 * math.sin(a0), h), (r1 * math.cos(a1), r1 * math.sin(a1), h)]
            N += [(0, 0, 1.0)] * 3
    if cap0 and abs(r0) > 1e-6:
        for i in range(seg):
            a0, a1 = ang[i], ang[i + 1]
            P += [(0, 0, 0.0), (r0 * math.cos(a1), r0 * math.sin(a1), 0), (r0 * math.cos(a0), r0 * math.sin(a0), 0)]
            N += [(0, 0, -1.0)] * 3
    return _arr(P), _arr(N)


def box(w, h, d):
    x, y = w / 2.0, h / 2.0
    v = [(-x, -y, 0), (x, -y, 0), (x, y, 0), (-x, y, 0), (-x, -y, d), (x, -y, d), (x, y, d), (-x, y, d)]
    faces = [((0, 3, 2, 1), (0, 0, -1.0)), ((4, 5, 6, 7), (0, 0, 1.0)), ((0, 1, 5, 4), (0, -1.0, 0)),
             ((2, 3, 7, 6), (0, 1.0, 0)), ((1, 2, 6, 5), (1.0, 0, 0)), ((0, 4, 7, 3), (-1.0, 0, 0))]
    P = []
    N = []
    for idx, n in faces:
        a, b, c, d2 = [v[i] for i in idx]
        P += [a, b, c, a, c, d2]
        N += [n] * 6
    return _arr(P), _arr(N)


def disk(r, seg=48, z=0.0, up=1.0):
    P = []
    N = []
    ang = np.linspace(0, 2 * math.pi, seg + 1)
    for i in range(seg):
        a0, a1 = ang[i], ang[i + 1]
        if up > 0:
            P += [(0, 0, z), (r * math.cos(a0), r * math.sin(a0), z), (r * math.cos(a1), r * math.sin(a1), z)]
        else:
            P += [(0, 0, z), (r * math.cos(a1), r * math.sin(a1), z), (r * math.cos(a0), r * math.sin(a0), z)]
        N += [(0, 0, up)] * 3
    return _arr(P), _arr(N)


def tube(ro, ri, h, seg=48):
    ang = np.linspace(0, 2 * math.pi, seg + 1)
    P = []
    N = []
    for i in range(seg):
        a0, a1 = ang[i], ang[i + 1]
        c0, s0 = math.cos(a0), math.sin(a0)
        c1, s1 = math.cos(a1), math.sin(a1)
        P += [(ro * c0, ro * s0, 0), (ro * c1, ro * s1, 0), (ro * c1, ro * s1, h), (ro * c0, ro * s0, 0), (ro * c1, ro * s1, h), (ro * c0, ro * s0, h)]
        N += [(c0, s0, 0), (c1, s1, 0), (c1, s1, 0), (c0, s0, 0), (c1, s1, 0), (c0, s0, 0)]
        P += [(ri * c0, ri * s0, 0), (ri * c1, ri * s1, h), (ri * c1, ri * s1, 0), (ri * c0, ri * s0, 0), (ri * c0, ri * s0, h), (ri * c1, ri * s1, h)]
        N += [(-c0, -s0, 0), (-c1, -s1, 0), (-c1, -s1, 0), (-c0, -s0, 0), (-c0, -s0, 0), (-c1, -s1, 0)]
        P += [(ri * c0, ri * s0, h), (ro * c0, ro * s0, h), (ro * c1, ro * s1, h), (ri * c0, ri * s0, h), (ro * c1, ro * s1, h), (ri * c1, ri * s1, h)]
        N += [(0, 0, 1.0)] * 6
        P += [(ri * c0, ri * s0, 0), (ro * c1, ro * s1, 0), (ro * c0, ro * s0, 0), (ri * c0, ri * s0, 0), (ri * c1, ri * s1, 0), (ro * c1, ro * s1, 0)]
        N += [(0, 0, -1.0)] * 6
    return _arr(P), _arr(N)


def rotmat(axis, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    if axis == "x":
        return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], "f4")
    if axis == "y":
        return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], "f4")
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], "f4")


def transform(P, N, R=None, t=None):
    P2 = (P @ R.T) if R is not None else P.copy()
    if t is not None:
        P2 = P2 + np.asarray(t, dtype=np.float32)
    N2 = (N @ R.T) if R is not None else N
    return P2.astype("f4"), N2.astype("f4")


PALETTE = {
    "red": (0.85, 0.18, 0.14), "white": (0.86, 0.88, 0.90), "dark": (0.12, 0.13, 0.14),
    "metal": (0.55, 0.57, 0.60), "black": (0.07, 0.08, 0.09), "accent": (0.78, 0.20, 0.16),
    "glass": (0.05, 0.08, 0.16),
}


# --------------------------------------------------------------------------- #
#  placeholder meshes (replaced by the modelling agents' build_* functions)   #
# --------------------------------------------------------------------------- #
def build_mount(C):
    import numpy as np, math
    parts = []

    # ---------- palette ----------
    BODY  = (0.12, 0.13, 0.14)   # dark graphite housing
    BODY2 = (0.19, 0.20, 0.21)   # lighter machined faces
    RED   = (0.80, 0.16, 0.13)   # red anodized
    RED2  = (0.87, 0.23, 0.18)   # brighter red highlight
    BLACK = (0.05, 0.05, 0.06)   # knobs / rubber
    BOLT  = (0.45, 0.47, 0.50)   # bright metal bolts
    METAL = (0.56, 0.58, 0.61)   # bright machined metal (shafts/axes)
    GLASS = (0.30, 0.55, 0.85)   # bubble level fluid
    WHITE = (0.85, 0.86, 0.88)   # labels / scale ticks

    SEG = 56

    def add(geom, col, R=None, t=None):
        P, N = geom
        if R is not None or t is not None:
            P, N = transform(P, N, R=R, t=t)
        parts.append((P, N, col))

    def ring_of_bolts(n, radius, center, axis, br=0.16, bh=0.20, col=BOLT, phase=0.0):
        """n cylindrical bolt heads on a circle, the bolts pointing along +axis."""
        for k in range(n):
            a = phase + 2*math.pi*k/n
            ca, sa = math.cos(a), math.sin(a)
            if axis == 'z':
                off = (radius*ca, radius*sa, 0.0); R = None
            elif axis == 'x':
                off = (0.0, radius*ca, radius*sa); R = rotmat('y', 90)
            else:  # 'y'
                off = (radius*ca, 0.0, radius*sa); R = rotmat('x', -90)
            t = (center[0]+off[0], center[1]+off[1], center[2]+off[2])
            add(cylinder(br, br, bh, seg=16), col, R=R, t=t)

    # =================================================================
    # 1) LATITUDE BASE (纬度座) — RED, bottom of mount, z ~ [-3, 3].
    #    A cradle/wedge whose tripod bottom face is tilted ~50deg about X.
    #    Altitude knob points -Y, curved altitude scale faces +X.
    # =================================================================
    # The base is designed in the HORIZONTAL (tripod) frame — z straight up,
    # platform flat on the tripod hub — then pre-rotated by +TILT about X so
    # that the scene's polar tilt (rotmat x, lat-90) brings it back level.
    TILT = 50.0                      # = 90 - design latitude (40)
    RB = rotmat('x', TILT)

    def addH(geom, col, R=None, t=(0.0, 0.0, 0.0)):
        Rh = RB if R is None else (RB @ R)
        th = RB @ np.asarray(t, dtype=np.float32)
        add(geom, col, R=Rh, t=tuple(float(v) for v in th))

    # flat azimuth platform resting on the tripod hub (top of hub = z_H 0)
    addH(box(7.6, 6.8, 1.1), RED, t=(0.0, 0.6, 0.0))
    addH(cylinder(2.9, 2.9, 0.45, seg=SEG), BODY2, t=(0.0, 0.6, 1.1))   # azimuth bearing ring
    # wedge block carrying the cradle
    addH(box(6.2, 5.2, 2.0), RED2, t=(0.0, 0.4, 1.55))

    # two cradle cheeks hugging the body's lower end (lean with the polar axis)
    for sx in (-3.55, 3.55):
        add(box(0.9, 5.2, 5.0), RED, t=(sx, -0.3, 2.6))

    # curved ALTITUDE scale on the +X cheek (red arc + white ticks)
    add(tube(3.1, 2.55, 0.6, seg=SEG), RED2, R=rotmat('y', 90), t=(4.05, -0.3, 4.6))
    for k in range(9):
        ang = math.radians(-40 + k * 10.0)
        add(box(0.12, 0.10, 0.4), WHITE, R=rotmat('x', math.degrees(ang)),
            t=(4.15, -0.3 + 2.85 * math.cos(ang), 4.6 + 2.85 * math.sin(ang)))

    # big knurled ALTITUDE bolt at the back (south, horizontal) + azimuth pair
    RaltH = rotmat('x', 90)          # cylinder +Z -> -Y (south)
    addH(cylinder(0.32, 0.32, 1.5, seg=20), METAL, R=RaltH, t=(0.0, -2.8, 1.4))
    addH(cylinder(0.95, 0.95, 1.6, seg=32), BLACK, R=RaltH, t=(0.0, -3.0, 1.4))
    addH(cylinder(1.06, 1.06, 0.45, seg=32), BLACK, R=RaltH, t=(0.0, -4.6, 1.4))
    addH(tube(0.55, 0.36, 0.30, seg=24), RED, R=RaltH, t=(0.0, -2.95, 1.4))
    for sx in (-1.5, 1.5):
        addH(cylinder(0.38, 0.38, 0.6, seg=20), BLACK, R=RaltH, t=(sx, -2.9, 0.55))
        addH(cylinder(0.16, 0.16, 0.9, seg=14), METAL, R=RaltH, t=(sx, -2.7, 0.55))

    _base_end = len(parts)  # parts so far = latitude base -> 'static' group

    # =================================================================
    # 2) MAIN BODY (方形本体) — RA / polar-axis strain-wave housing.
    #    Rounded-rectangular housing running along +Z, z in [3, 17].
    # =================================================================
    BW, BD = 6.4, 6.0       # body cross-section (x, y)
    Z0, Z1 = 4.0, 17.0
    BL = Z1 - Z0

    # core housing
    add(box(BW, BD, BL), BODY, t=(0.0, 0.0, Z0))
    # rounded vertical corner fillets
    cr = 0.85
    for sx in (-1, 1):
        for sy in (-1, 1):
            cx = sx*(BW/2 - cr*0.55)
            cy = sy*(BD/2 - cr*0.55)
            add(cylinder(cr, cr, BL, seg=24), BODY, t=(cx, cy, Z0))
    # lighter machined shoulder collars top & bottom
    add(box(BW + 0.3, BD + 0.3, 1.0), BODY2, t=(0.0, 0.0, Z0))
    add(box(BW + 0.3, BD + 0.3, 1.1), BODY2, t=(0.0, 0.0, Z1 - 1.1))

    # machined bevel strips on +X / -X long faces
    for sx, R in ((BW/2 + 0.02, rotmat('y', 90)), (-BW/2 - 0.02, rotmat('y', -90))):
        add(box(BL - 1.4, BD - 1.6, 0.18), BODY2, R=R, t=(sx, 0.0, Z0 + BL/2))

    # -- AM5 label panel on the +Y face (lighter recessed plate + red bar)
    add(box(3.8, 3.0, 0.12), BODY2, R=rotmat('x', -90), t=(0.0, BD/2 + 0.08, 9.6))
    add(box(3.2, 0.42, 0.10), RED, R=rotmat('x', -90), t=(0.0, BD/2 + 0.16, 10.4))   # red AM5 bar
    for sx in (-0.95, 0.0, 0.95):                                                    # white "A M 5" blocks
        add(box(0.5, 0.7, 0.08), WHITE, R=rotmat('x', -90), t=(sx, BD/2 + 0.16, 9.4))

    # -- ports / power panel on the -Y face (recessed black plate + connectors)
    add(box(2.8, 2.2, 0.14), BLACK, R=rotmat('x', 90), t=(0.7, -BD/2 - 0.08, 6.2))
    for sx in (0.0, 0.7, 1.4):
        add(cylinder(0.28, 0.28, 0.22, seg=20), METAL, R=rotmat('x', 90), t=(sx, -BD/2 - 0.10, 6.7))
    add(box(0.9, 0.45, 0.18), BODY2, R=rotmat('x', 90), t=(0.7, -BD/2 - 0.10, 5.6))   # USB/12V port

    # -- bubble level on -Y face near top
    add(cylinder(0.55, 0.55, 0.22, seg=28), BLACK, R=rotmat('x', 90), t=(-1.7, -BD/2 - 0.08, 7.0))
    add(cylinder(0.40, 0.40, 0.26, seg=28), GLASS, R=rotmat('x', 90), t=(-1.7, -BD/2 - 0.12, 7.0))
    add(tube(0.42, 0.30, 0.10, seg=28), BODY2, R=rotmat('x', 90), t=(-1.7, -BD/2 - 0.30, 7.0))

    # =================================================================
    # 3a) RA AXIS OUTPUT (赤经轴) — polar axis exits the TOP of body (+Z).
    #     metal hub + red accent ring + 6-bolt end cap.  Carries Dec elbow.
    # =================================================================
    add(cylinder(2.6, 2.55, 0.7, seg=SEG), BODY2, t=(0.0, 0.0, Z1))        # output flange
    add(tube(2.6, 2.2, 0.5, seg=SEG), RED, t=(0.0, 0.0, Z1 + 0.45))        # red accent ring
    add(cylinder(2.3, 2.3, 0.35, seg=SEG), BODY2, t=(0.0, 0.0, Z1 + 0.85)) # end cap disk
    ring_of_bolts(6, 1.55, (0.0, 0.0, Z1 + 1.2), 'z', br=0.15, bh=0.14, col=BOLT)
    add(cylinder(0.45, 0.45, 0.18, seg=20), BOLT, t=(0.0, 0.0, Z1 + 1.2))  # central RA bolt

    # =================================================================
    # 3b) DEC CARRIER ELBOW — rides on the RA output, holds the Dec gearbox.
    #     Block atop the body, offset so the Dec axis center is z ~ 15.3.
    # =================================================================
    DECZ = 15.3
    add(box(5.2, 5.6, 4.8), BODY, t=(0.3, 0.0, 12.6))        # elbow block (overlaps body top)
    add(box(5.6, 5.9, 0.7), BODY2, t=(0.3, 0.0, 16.6))       # lighter top plate
    # machined cheek on +X side of the elbow (where Dec axis exits)
    add(box(0.18, 5.0, 3.6), BODY2, R=rotmat('y', 90), t=(0.3 + 5.2/2 + 0.02, 0.0, DECZ))

    # =================================================================
    # 3c) DEC AXIS (赤纬轴) — thick cylindrical harmonic gearbox along +X,
    #     at z ~ 15.3, reaching to x ~ +8.
    # =================================================================
    Rdec = rotmat('y', 90)   # cylinder +Z -> +X
    add(cylinder(2.55, 2.55, 6.6, seg=60), BODY, R=Rdec, t=(1.4, 0.0, DECZ))   # Dec barrel
    add(tube(2.62, 2.3, 0.9, seg=60), BODY2, R=Rdec, t=(1.6, 0.0, DECZ))       # machined band (body side)
    add(tube(2.6, 2.25, 0.6, seg=60), RED, R=Rdec, t=(5.2, 0.0, DECZ))         # red accent ring (outboard)
    # Dec end hub (lighter) + outboard output flange facing +X (saddle bolts on it)
    add(cylinder(2.35, 2.35, 0.9, seg=60), BODY2, R=Rdec, t=(8.0, 0.0, DECZ))
    add(disk(2.3, seg=60, up=1.0), BODY2, R=Rdec, t=(8.9, 0.0, DECZ))

    # =================================================================
    # 4) SADDLE / DOVETAIL CLAMP (鞍座) — RED, on the Dec axis at +X end (~x=6.5).
    #    Dec=90 home position: dovetail SLOT runs ALONG +Z, OPENS toward +Y.
    #    A scope (optical axis +Z) with an underside dovetail drops in from +Y
    #    and ends up offset on the +Y side.
    # =================================================================
    _saddle_start = len(parts)  # parts from here = saddle on the Dec output -> 'dec' group

    # Saddle bolted flat on the Dec OUTPUT flange (x = 8.9): plate normal +X,
    # dovetail slot runs along +Z and OPENS toward +X (outboard along the Dec
    # axis). The scope's underside dovetail drops on from +X, so the OTA
    # centerline sits ON the Dec axis line (y=0, z=DECZ) — like the real AM5.
    SXP = 8.9                         # Dec output flange plane
    SLOT_LEN = 7.0
    Zs = DECZ - SLOT_LEN / 2.0        # slot z-start

    # red mounting plate on the output flange
    add(box(1.0, 6.4, SLOT_LEN), RED, t=(SXP + 0.5, 0.0, Zs))
    # two jaw rails flanking the dovetail bar (bar is 4.4 wide -> jaws at |y|>=2.2)
    for sy in (-1, 1):
        add(box(1.15, 1.0, SLOT_LEN), RED, t=(SXP + 1.0 + 0.575, sy * 2.7, Zs))
    # angled dovetail lips leaning inward over the bar's top edges
    for sy in (-1, 1):
        add(box(0.5, 0.75, SLOT_LEN - 0.4), BODY2, R=rotmat('z', -sy * 24), t=(SXP + 1.85, sy * 2.35, Zs + 0.2))

    # big clamp KNOB on the +Y jaw, pointing +Y
    Rkn = rotmat('x', -90)            # cylinder +Z -> +Y
    add(cylinder(0.30, 0.30, 1.2, seg=20), METAL, R=Rkn, t=(SXP + 1.6, 3.2, DECZ - 1.5))
    add(cylinder(0.92, 0.92, 1.5, seg=32), BLACK, R=Rkn, t=(SXP + 1.6, 3.6, DECZ - 1.5))
    add(cylinder(1.04, 1.04, 0.45, seg=32), BLACK, R=Rkn, t=(SXP + 1.6, 5.1, DECZ - 1.5))
    add(tube(0.6, 0.32, 0.3, seg=24), RED2, R=Rkn, t=(SXP + 1.6, 3.62, DECZ - 1.5))

    # safety screw on the -Y jaw, pointing -Y
    add(cylinder(0.26, 0.26, 0.85, seg=16), BOLT, R=rotmat('x', 90), t=(SXP + 1.6, -3.2, DECZ + 1.6))
    add(cylinder(0.40, 0.40, 0.25, seg=16), BLACK, R=rotmat('x', 90), t=(SXP + 1.6, -3.95, DECZ + 1.6))
    # safety stop at the -Z end of the slot
    add(box(1.0, 2.3, 0.45), BOLT, t=(SXP + 1.5, 0.0, Zs - 0.45))

    # Tag the kinematic group of every part: the latitude base is static, the
    # square body (RA element) turns about the polar axis, the saddle rides the
    # Dec output and turns with the telescope.
    tagged = []
    for i, (P, N, col) in enumerate(parts):
        grp = "static" if i < _base_end else ("dec" if i >= _saddle_start else "ra")
        tagged.append((P, N, col, grp))
    return tagged


def build_scope(C):
    parts = []

    # ---- color palette ----
    white   = (0.86, 0.88, 0.90)   # tube / dew shield
    shield  = (0.83, 0.85, 0.87)   # dew shield slightly darker
    dark    = (0.10, 0.11, 0.12)   # rings, dovetail, focuser body
    black   = (0.05, 0.05, 0.06)   # knobs
    chrome  = (0.70, 0.72, 0.74)   # drawtube
    accent  = (0.10, 0.30, 0.62)   # blue accent ring
    glass   = (0.05, 0.08, 0.16)   # objective glass
    brass   = (0.72, 0.58, 0.22)   # tension screw
    plate   = (0.78, 0.80, 0.82)   # nameplate band (silver)

    SEG = 56

    def add(P, N, col, R=None, t=None):
        if R is not None or t is not None:
            P, N = transform(P, N, R, t)
        parts.append((P, N, col))

    # =====================================================================
    # MAIN TUBE BODY  -- centered around origin along Z
    #   tube outer radius ~5.1 cm, body length ~41 cm => z in [-20.5, 20.5]
    # =====================================================================
    R_TUBE = 5.1
    L_TUBE = 41.0
    z_tube0 = -L_TUBE/2.0   # -20.5
    z_tube1 =  L_TUBE/2.0   #  20.5

    # main white tube wall (hollow so interior reads dark at ends)
    P,N = tube(R_TUBE, R_TUBE-0.25, L_TUBE, seg=SEG)
    add(P,N, white, t=(0,0,z_tube0))

    # inner dark liner (baffled interior)
    P,N = cylinder(R_TUBE-0.26, R_TUBE-0.26, L_TUBE, seg=SEG, cap0=False, cap1=False)
    add(P,N, (0.03,0.03,0.035), t=(0,0,z_tube0))

    # subtle engraved nameplate band (silver) near front third of tube
    P,N = tube(R_TUBE+0.04, R_TUBE-0.05, 3.2, seg=SEG)
    add(P,N, plate, t=(0,0, z_tube1-12.0))

    # thin colored accent ring/band near the front of the tube
    P,N = tube(R_TUBE+0.10, R_TUBE-0.05, 0.7, seg=SEG)
    add(P,N, accent, t=(0,0, z_tube1-1.4))

    # a second thin accent pinstripe just behind it
    P,N = tube(R_TUBE+0.06, R_TUBE-0.05, 0.18, seg=SEG)
    add(P,N, accent, t=(0,0, z_tube1-2.6))

    # =====================================================================
    # DEW SHIELD  -- front (+Z), slightly larger OD than tube
    #   OD ~5.8 radius, length ~11 cm, sits over front of tube
    # =====================================================================
    R_SHIELD = 5.8
    L_SHIELD = 11.0
    z_sh0 = z_tube1 - 1.5            # overlaps tube front by ~1.5cm
    z_sh1 = z_sh0 + L_SHIELD

    # dew shield wall (hollow)
    P,N = tube(R_SHIELD, R_SHIELD-0.30, L_SHIELD, seg=SEG)
    add(P,N, shield, t=(0,0,z_sh0))

    # retraction seam line (a thin recessed ring groove) around mid of shield
    P,N = tube(R_SHIELD-0.06, R_SHIELD-0.32, 0.22, seg=SEG)
    add(P,N, (0.55,0.56,0.58), t=(0,0, z_sh0+0.9))

    # front lip ring (thicker band at the very front mouth)
    P,N = tube(R_SHIELD+0.18, R_SHIELD-0.35, 0.9, seg=SEG)
    add(P,N, shield, t=(0,0, z_sh1-0.9))

    # inner dark wall of dew shield (anti-reflection flocking)
    P,N = cylinder(R_SHIELD-0.32, R_SHIELD-0.32, L_SHIELD, seg=SEG, cap0=False, cap1=False)
    add(P,N, (0.02,0.02,0.025), t=(0,0,z_sh0))

    # =====================================================================
    # OBJECTIVE LENS CELL  -- recessed inside the dew shield
    #   dark lens cell ring + dark-blue glass disk
    # =====================================================================
    z_lens = z_tube1 - 0.4   # recessed ~ near tube front, well inside shield
    # lens cell retaining ring (dark metal)
    P,N = tube(R_TUBE-0.10, R_TUBE-1.10, 0.8, seg=SEG)
    add(P,N, dark, t=(0,0, z_lens))
    # dark blue objective glass disk (slightly recessed, facing +Z)
    P,N = disk(R_TUBE-1.10, seg=SEG, z=z_lens+0.35, up=1.0)
    add(P,N, glass)
    # a faint inner secondary reflection ring on glass
    P,N = disk(R_TUBE-2.6, seg=SEG, z=z_lens+0.36, up=1.0)
    add(P,N, (0.09,0.14,0.28))

    # =====================================================================
    # TUBE RINGS  -- two hinged rings clamping the tube
    #   each: a band around tube + hinge boss + clamp knob
    #   positioned bottom -Y; bolted down to dovetail
    # =====================================================================
    R_RING_O = R_TUBE + 0.6
    RING_W = 2.6
    ring_zs = [z_tube0 + 9.0, z_tube1 - 13.0]   # two ring centers along Z

    def build_ring(zc):
        # ring band (hollow, clamps tube)
        P,N = tube(R_RING_O, R_TUBE-0.02, RING_W, seg=SEG)
        add(P,N, dark, t=(0,0, zc - RING_W/2.0))
        # hinge pin (small cylinder) across the ring's +X side
        Pp,Np = cylinder(0.35,0.35, RING_W*0.9, seg=20, cap0=True, cap1=True)
        Pp,Np = transform(Pp,Np,rotmat('y', 90),(0,0,0))  # axis along X
        add(Pp,Np, (0.06,0.06,0.07), t=(R_RING_O-0.1, 0.0, zc - RING_W*0.45))
        # clamp boss block on -X side (split/clamp tabs)
        P,N = box(0.9, 1.8, RING_W*0.75)
        add(P,N, dark, t=(-R_RING_O-0.0, 0.0, zc - (RING_W*0.75)/2.0))
        # clamp knob (black) on -X side sticking out
        Rk = rotmat('y', -90)  # axis along -X
        Pk,Nk = cylinder(0.55,0.62, 1.1, seg=24)
        Pk,Nk = transform(Pk,Nk,Rk,(0,0,0))
        add(Pk,Nk, black, t=(-R_RING_O-0.6, 0.0, zc))
        # knob cap
        Pc,Nc = cylinder(0.62,0.50, 0.25, seg=24)
        Pc,Nc = transform(Pc,Nc,Rk,(0,0,0))
        add(Pc,Nc, black, t=(-R_RING_O-1.7, 0.0, zc))

    for zc in ring_zs:
        build_ring(zc)

    # =====================================================================
    # DOVETAIL BAR  -- along the bottom (-Y), runs along Z
    #   Vixen-style: trapezoid-ish bar; bottom face is the saddle clamp
    # =====================================================================
    DT_LEN = 30.0
    DT_W   = 4.4    # x width (top)
    DT_H   = 1.5    # y height
    dt_top_y = -(R_RING_O)        # touches bottom of rings ~ -5.7
    dt_bot_y = dt_top_y - DT_H    # bottom face = -7.2
    dt_zc = (ring_zs[0] + ring_zs[1]) / 2.0
    dt_z0 = dt_zc - DT_LEN/2.0

    # main bar (box). box z in [0,d] along Z, centered in x,y.
    P,N = box(DT_W, DT_H, DT_LEN)
    add(P,N, dark, t=(0, dt_bot_y + DT_H/2.0, dt_z0))

    # dovetail beveled lower rail (narrower) to suggest trapezoid
    P,N = box(DT_W-1.4, 0.5, DT_LEN)
    add(P,N, (0.07,0.07,0.08), t=(0, dt_bot_y + 0.25, dt_z0))

    # recessed bolt circles on underside connecting to rings (two)
    for zc in ring_zs:
        Pb,Nb = cylinder(0.5,0.5,0.4, seg=20)
        Pb,Nb = transform(Pb,Nb,rotmat('x',-90),(0,0,0))
        add(Pb,Nb, (0.04,0.04,0.045), t=(0, dt_bot_y+0.02, zc))

    # =====================================================================
    # SHORT TOP HANDLE / DOVETAIL  -- on top (+Y)
    # =====================================================================
    TH_LEN = 14.0
    TH_W = 3.2
    TH_H = 1.1
    th_bot_y = R_RING_O
    th_z0 = dt_zc - TH_LEN/2.0
    P,N = box(TH_W, TH_H, TH_LEN)
    add(P,N, dark, t=(0, th_bot_y + TH_H/2.0, th_z0))
    # finger groove rail on top
    P,N = box(TH_W-1.2, 0.4, TH_LEN)
    add(P,N, (0.07,0.07,0.08), t=(0, th_bot_y + TH_H - 0.05, th_z0))

    # =====================================================================
    # FOCUSER  -- at back (-Z): rotator, housing, drawtube, knobs, screw
    # =====================================================================
    # rotator collar between tube back and focuser body
    P,N = cylinder(R_TUBE+0.2, R_TUBE+0.2, 1.4, seg=SEG)
    add(P,N, dark, t=(0,0, z_tube0 - 1.4))
    # rotator knurl ring
    P,N = tube(R_TUBE+0.45, R_TUBE-0.2, 0.9, seg=SEG)
    add(P,N, (0.06,0.06,0.07), t=(0,0, z_tube0 - 1.1))

    z_foc1 = z_tube0 - 1.4          # focuser body front
    FOC_BODY_L = 6.0
    z_foc0 = z_foc1 - FOC_BODY_L    # focuser body back = -27.9

    # focuser body (cylindrical base)
    P,N = cylinder(R_TUBE-0.1, R_TUBE-0.4, FOC_BODY_L, seg=SEG)
    add(P,N, dark, t=(0,0, z_foc0))
    # squared housing box around the focuser (rack & pinion block)
    HB_W = (R_TUBE+0.2)*2*0.9
    P,N = box(HB_W, HB_W, FOC_BODY_L*0.85)
    add(P,N, dark, t=(0, 0, z_foc0 + FOC_BODY_L*0.075))

    # chrome drawtube sticking out the back
    DRAW_L = 4.0
    R_DRAW = 3.2
    z_draw1 = z_foc0 - DRAW_L        # = -31.9
    P,N = cylinder(R_DRAW, R_DRAW, DRAW_L, seg=SEG, cap0=True, cap1=False)
    add(P,N, chrome, t=(0,0, z_draw1))
    # drawtube graduated scale ring (thin)
    P,N = tube(R_DRAW+0.04, R_DRAW-0.05, 0.15, seg=SEG)
    add(P,N, (0.2,0.2,0.22), t=(0,0, z_draw1+1.2))

    # rear M68/2" opening: end ring + recessed dark bore (camera mates here)
    R_OPEN_O = 3.2
    R_OPEN_I = 2.45     # ~2" opening radius
    z_open = z_draw1    # rear-most opening plane ~ -31.9
    # rear face ring (metal annulus around the opening), front face at z_open-0.6 = -32.5
    P,N = tube(R_OPEN_O, R_OPEN_I, 0.6, seg=SEG)
    add(P,N, (0.08,0.08,0.09), t=(0,0, z_open-0.6))
    # inner threaded bore (dark)
    P,N = cylinder(R_OPEN_I, R_OPEN_I, 1.6, seg=SEG, cap0=False, cap1=False)
    add(P,N, (0.02,0.02,0.025), t=(0,0, z_open))
    # dark inner disk closing it off
    P,N = disk(R_OPEN_I, seg=SEG, z=z_open+1.4, up=-1.0)
    add(P,N, (0.02,0.02,0.025))

    # ---- TWO coarse focus knobs (left/right, +X and -X) ----
    z_knob = z_foc0 + FOC_BODY_L*0.55   # focus knob axis position along Z
    knob_y = -1.2
    for sx in (+1, -1):
        Rk = rotmat('y', 90) if sx>0 else rotmat('y',-90)  # axis along X
        # knob shaft
        Ps,Ns = cylinder(0.4,0.4, 1.0, seg=20)
        Ps,Ns = transform(Ps,Ns,Rk,(0,0,0))
        add(Ps,Ns, (0.06,0.06,0.07), t=(sx*(HB_W/2.0+0.0), knob_y, z_knob))
        # coarse knob body (knurled black cylinder)
        Pk,Nk = cylinder(1.05,1.05, 1.3, seg=28)
        Pk,Nk = transform(Pk,Nk,Rk,(0,0,0))
        add(Pk,Nk, black, t=(sx*(HB_W/2.0+1.0), knob_y, z_knob))
        # knurl grip ring on knob
        Pg,Ng = tube(1.12,0.9, 0.8, seg=28)
        Pg,Ng = transform(Pg,Ng,Rk,(0,0,0))
        add(Pg,Ng, (0.03,0.03,0.035), t=(sx*(HB_W/2.0+1.2), knob_y, z_knob))
        # outer cap
        Pc,Nc = cylinder(1.05,0.7, 0.3, seg=28)
        Pc,Nc = transform(Pc,Nc,Rk,(0,0,0))
        add(Pc,Nc, black, t=(sx*(HB_W/2.0+2.3), knob_y, z_knob))

    # ---- fine-focus knob coaxial (smaller, right side, outboard of coarse) ----
    sx = +1
    Rk = rotmat('y', 90)
    Pf,Nf = cylinder(0.6,0.6, 0.9, seg=24)
    Pf,Nf = transform(Pf,Nf,Rk,(0,0,0))
    add(Pf,Nf, (0.08,0.08,0.09), t=(sx*(HB_W/2.0+2.6), knob_y, z_knob))
    Pf2,Nf2 = cylinder(0.6,0.45, 0.25, seg=24)
    Pf2,Nf2 = transform(Pf2,Nf2,Rk,(0,0,0))
    add(Pf2,Nf2, black, t=(sx*(HB_W/2.0+3.5), knob_y, z_knob))

    # ---- brass tension/lock screw on top of focuser body (+Y) ----
    Rt = rotmat('x', -90)  # axis along +Y
    Pt,Nt = cylinder(0.42,0.42, 1.0, seg=20)
    Pt,Nt = transform(Pt,Nt,Rt,(0,0,0))
    add(Pt,Nt, brass, t=(0, HB_W/2.0, z_foc0 + FOC_BODY_L*0.3))
    # brass screw head
    Pth,Nth = cylinder(0.55,0.42, 0.3, seg=20)
    Pth,Nth = transform(Pth,Nth,Rt,(0,0,0))
    add(Pth,Nth, brass, t=(0, HB_W/2.0+1.0, z_foc0 + FOC_BODY_L*0.3))

    return parts


def build_camera(C):
    # ZWO ASI6200 PRO full-frame cooled astronomy camera.
    # Local frame: optical axis = +Z. Front mating face at z=0 facing +Z.
    # Body extends toward -Z; fan at the most negative Z.
    parts = []

    # ---- Colors ----
    RED       = C.get("red",   (0.85, 0.18, 0.14))   # bright ZWO red anodized body
    RED_FIN   = (0.72, 0.16, 0.13)                   # slightly darker anodized fins
    RED_DEEP  = (0.66, 0.14, 0.11)                   # deepest red shadow ring
    SILVER    = (0.62, 0.64, 0.66)                   # front collar silver/dark
    BLACK     = C.get("black", (0.05, 0.05, 0.06))   # thread / ports
    THREAD    = (0.10, 0.10, 0.11)                   # M48 thread stub
    SENSORDK  = (0.02, 0.02, 0.03)                   # very dark sensor window
    SENSORBLU = (0.04, 0.05, 0.09)                   # subtle sensor glint
    GLASS     = (0.10, 0.13, 0.16)                   # protective window glass tint
    SCREW     = (0.55, 0.57, 0.60)                   # adjustment screws
    FANBLADE  = (0.10, 0.11, 0.12)                   # dark grey fan blades
    HUB       = (0.03, 0.03, 0.04)                   # black fan hub
    GREYMETAL = (0.30, 0.31, 0.33)                   # fan grille / shroud accents
    PORTGOLD  = (0.55, 0.45, 0.20)                   # barrel jack contact hint

    def add(PN, color, R=None, t=None):
        P, N = PN
        if R is not None or t is not None:
            P, N = transform(P, N, R, t)
        parts.append((P, N, color))

    SEG = 56

    # =====================================================================
    # Geometry layout along Z (z<=0 except the thread stub at +Z):
    #   z = 0.0          : front mating face (M48 ring face)
    #   thread stub  : z in [0.0 .. +0.45]   (protrudes toward scope, +Z)
    #   tilt plate   : z in [-0.30 .. 0.0]
    #   front collar : z in [-1.10 .. -0.30]
    #   main red body: z in [-3.70 .. -1.10]
    #   cooling/fins : z in [-7.20 .. -3.70]
    #   fan housing  : z in [-9.60 .. -7.20] (fan back ~ -10.02)
    # =====================================================================

    R_BODY   = 3.9     # main body radius (78mm dia)
    R_COLLAR = 3.55    # front collar radius

    # ---------------------------------------------------------------
    # (1) FRONT: M48/T2 thread stub (protrudes toward +Z, into the scope)
    # ---------------------------------------------------------------
    add(tube(1.62, 1.30, 0.45, seg=SEG), THREAD, t=(0, 0, 0.0))
    for k in range(4):  # thread ridges
        zz = 0.06 + k * 0.10
        add(tube(1.66, 1.55, 0.035, seg=SEG), (0.14, 0.14, 0.15), t=(0, 0, zz))

    # ---------------------------------------------------------------
    # (1) FRONT face / silver mating ring + tilt-adjustment plate (z<=0)
    # ---------------------------------------------------------------
    add(tube(R_COLLAR, 1.30, 0.30, seg=SEG), SILVER, t=(0, 0, -0.30))
    add(tube(2.55, 1.65, 0.10, seg=SEG), (0.55, 0.57, 0.59), t=(0, 0, -0.02))

    # round window + protective glass recessed in the bore
    add(disk(1.28, seg=SEG, z=-0.18, up=1.0), GLASS)
    add(cylinder(1.28, 1.28, 0.02, seg=SEG, cap0=False, cap1=False), (0.18, 0.20, 0.22), t=(0, 0, -0.20))

    # SENSOR: dark square rectangle behind the window (full-frame 36x24mm),
    # scaled to fit inside the round window bore.
    sw, sh = 1.95, 1.30
    P, N = box(sw, sh, 0.04);          add((P, N), SENSORDK,  t=(0, 0, -0.58))
    P, N = box(sw*0.86, sh*0.86, 0.02);add((P, N), SENSORBLU, t=(0, 0, -0.55))
    # dark cavity wall from window down to sensor
    add(cylinder(1.26, 1.26, 0.40, seg=SEG, cap0=False, cap1=False), (0.03, 0.03, 0.04), t=(0, 0, -0.58))

    # tilt-adjustment screws: 3 tiny screws around the front plate
    for i in range(3):
        ang = math.radians(90 + i * 120)
        rx = 2.05 * math.cos(ang); ry = 2.05 * math.sin(ang)
        add(cylinder(0.14, 0.12, 0.10, seg=20), SCREW, t=(rx, ry, -0.04))
        P, N = box(0.20, 0.04, 0.02)
        add((P, N), (0.20, 0.21, 0.22), R=rotmat('z', math.degrees(ang)), t=(rx, ry, 0.04))

    # ---------------------------------------------------------------
    # (1b) front collar transition (silver -> red)
    # ---------------------------------------------------------------
    add(cylinder(R_COLLAR, R_BODY, 0.80, seg=SEG, cap0=False, cap1=False), SILVER, t=(0, 0, -1.10))
    add(tube(R_BODY+0.04, R_BODY-0.10, 0.18, seg=SEG), (0.12, 0.12, 0.13), t=(0, 0, -1.28))

    # ---------------------------------------------------------------
    # (2) MAIN RED BODY: glossy red anodized cylinder  z in [-3.70 .. -1.10]
    # ---------------------------------------------------------------
    add(cylinder(R_BODY, R_BODY, 2.60, seg=SEG, cap0=True, cap1=False), RED, t=(0, 0, -3.70))
    add(tube(R_BODY+0.02, R_BODY-0.06, 0.08, seg=SEG), RED_DEEP, t=(0, 0, -1.55))
    # "ASI6200" engraving hint: a small recessed plaque on the body side
    P, N = box(1.5, 0.55, 0.03)
    add((P, N), (0.55, 0.12, 0.10), R=rotmat('y', 90), t=(R_BODY+0.005, 0, -2.55))

    # ---------------------------------------------------------------
    # (3) COOLING SECTION: heat-sink fins + side ports  z in [-7.20 .. -3.70]
    # ---------------------------------------------------------------
    z_cool_top = -3.70
    z_cool_bot = -7.20
    add(cylinder(R_BODY-0.05, R_BODY-0.05, (z_cool_top - z_cool_bot), seg=SEG, cap0=True, cap1=False),
        RED_FIN, t=(0, 0, z_cool_bot))
    # squared cooling block
    sq = (R_BODY*2 - 0.5)
    P, N = box(sq, sq, (z_cool_top - z_cool_bot) - 0.2)
    add((P, N), RED_DEEP, t=(0, 0, z_cool_bot+0.1))

    # heat-sink fins: 8 fins as thin rings slightly larger than the body
    R_FIN = R_BODY + 0.55
    n_fins = 8
    fin_zone_top = -4.00
    fin_zone_bot = -6.90
    fin_h = 0.16
    for i in range(n_fins):
        fz = fin_zone_top - i * (fin_zone_top - fin_zone_bot) / (n_fins - 1)
        col = RED_FIN if (i % 2 == 0) else RED_DEEP
        add(tube(R_FIN, R_BODY-0.12, fin_h, seg=SEG), col, t=(0, 0, fz - fin_h))

    # ---- SIDE PORTS (on +X face of the cooling section) ----
    # Ports built with local z in [0,d]; rotating y,90 maps +z -> +x, so
    # base_x + d = the outer (poking-out) face.
    boss_base_x = R_BODY - 0.10
    boss_d = 0.95
    P, N = box(2.7, 4.4, boss_d)
    add((P, N), (0.18, 0.18, 0.19), R=rotmat('y', 90), t=(boss_base_x, 0, z_cool_bot+1.7))
    boss_face_x = boss_base_x + boss_d  # ports start here

    # USB3 port (larger) + blue inner
    P, N = box(0.95, 1.45, 0.75)
    add((P, N), BLACK, R=rotmat('y', 90), t=(boss_face_x-0.02, -1.25, z_cool_bot+2.55))
    P, N = box(0.62, 1.10, 0.30)
    add((P, N), (0.10, 0.18, 0.45), R=rotmat('y', 90), t=(boss_face_x+0.10, -1.25, z_cool_bot+2.55))

    # 2x USB2 hub ports (smaller)
    for j, yy in enumerate([-0.05, 0.75]):
        P, N = box(0.75, 1.10, 0.55)
        add((P, N), BLACK, R=rotmat('y', 90), t=(boss_face_x-0.02, yy, z_cool_bot+1.85))
        P, N = box(0.45, 0.82, 0.22)
        add((P, N), (0.12, 0.12, 0.13), R=rotmat('y', 90), t=(boss_face_x+0.06, yy, z_cool_bot+1.85))

    # 12V power barrel jack (cylinder pointing +X)
    bx = boss_face_x - 0.10
    add(cylinder(0.36, 0.34, 0.55, seg=28), BLACK,            R=rotmat('y', 90), t=(bx, 1.55, z_cool_bot+0.95))
    add(cylinder(0.20, 0.20, 0.66, seg=22), (0.10, 0.10, 0.11), R=rotmat('y', 90), t=(bx, 1.55, z_cool_bot+0.95))
    add(cylinder(0.08, 0.08, 0.72, seg=16), PORTGOLD,         R=rotmat('y', 90), t=(bx, 1.55, z_cool_bot+0.95))

    # ---------------------------------------------------------------
    # (4) BACK: fan housing + recessed fan with angled blades + hub
    # ---------------------------------------------------------------
    z_fan_top = -7.20
    fan_house_h = 1.6
    z_fan_bot = z_fan_top - fan_house_h
    add(cylinder(R_BODY, R_BODY-0.2, 0.4, seg=SEG, cap0=False, cap1=False), RED_FIN, t=(0, 0, z_fan_top-0.4))
    add(cylinder(R_BODY-0.2, R_BODY-0.2, fan_house_h-0.4, seg=SEG, cap0=False, cap1=False), (0.20, 0.20, 0.21), t=(0, 0, z_fan_bot))

    # protective ring around the fan at the back
    add(tube(R_BODY-0.05, R_BODY-0.85, 0.30, seg=SEG), (0.16, 0.16, 0.17), t=(0, 0, z_fan_bot-0.30))

    # recessed back plate
    z_back = z_fan_bot - 0.05
    add(disk(R_BODY-0.85, seg=SEG, z=z_back, up=-1.0), (0.08, 0.08, 0.09))

    # fan grille spokes
    n_spokes = 4
    for i in range(n_spokes):
        ang = math.radians(i * 180.0 / n_spokes)
        P, N = box(2*(R_BODY-0.9), 0.18, 0.10)
        add((P, N), GREYMETAL, R=rotmat('z', math.degrees(ang)), t=(0, 0, z_back-0.12))

    # FAN blades: 8 angled blades around a central hub, recessed
    z_blade = z_back - 0.55
    n_blades = 8
    R_blade_out = R_BODY - 1.0
    R_blade_in = 0.55
    for i in range(n_blades):
        ang = i * 360.0 / n_blades
        P, N = box(R_blade_out - R_blade_in, 1.0, 0.10)
        Rp = rotmat('y', 32)  # blade pitch / angle of attack
        Pt, Nt = transform(P, N, Rp, ((R_blade_out + R_blade_in)/2.0, 0, 0))
        add((Pt, Nt), FANBLADE, R=rotmat('z', ang), t=(0, 0, z_blade))

    # center HUB
    add(cylinder(0.62, 0.55, 0.85, seg=32), HUB, t=(0, 0, z_blade-0.30))
    add(disk(0.55, seg=32, z=z_blade-0.30, up=-1.0), (0.02, 0.02, 0.03))
    add(cylinder(0.18, 0.10, 0.20, seg=16), (0.30, 0.30, 0.32), t=(0, 0, z_blade-0.45))

    # outer back rim cap ring
    add(tube(R_BODY-0.05, R_BODY-0.30, 0.18, seg=SEG), (0.18, 0.18, 0.19), t=(0, 0, z_fan_bot-0.62))

    return parts


def build_scene(ra_hours, dec_degrees, lst_hours=None, pier_side="pier_east", latitude=40.0, ha_override=None):
    C = PALETTE
    parts = []
    apex = np.array([0, 0, 22.0])
    # vertical pier column (立柱) — fixed observatory pier instead of a tripod
    PIER = (0.30, 0.32, 0.35)

    def _add(geom, col, t):
        p0, n0 = geom
        parts.append((*transform(p0, n0, None, t), col))

    _add(cylinder(8.0, 8.0, 0.9, seg=56), C["dark"], (0, 0, 0.0))            # ground base plate
    _add(tube(8.0, 6.6, 0.35, seg=56), (0.19, 0.20, 0.21), (0, 0, 0.9))      # machined bevel ring
    for k in range(6):                                                        # anchor bolts
        a = math.radians(60 * k + 30)
        _add(cylinder(0.35, 0.35, 0.4, seg=14), C["black"], (7.1 * math.cos(a), 7.1 * math.sin(a), 0.9))
    _add(cylinder(4.6, 3.4, 1.6, seg=48), C["dark"], (0, 0, 0.9))            # tapered column root
    _add(cylinder(3.4, 3.4, 17.5, seg=48), PIER, (0, 0, 2.5))                # main column
    _add(tube(3.52, 3.1, 0.5, seg=48), (0.80, 0.16, 0.13), (0, 0, 19.4))     # red accent ring
    _add(cylinder(4.2, 4.2, 1.0, seg=48), C["dark"], (0, 0, 20.0))           # top flange
    _add(cylinder(2.8, 2.8, 1.0, seg=36), (0.19, 0.20, 0.21), (0, 0, 21.0))  # neck
    p, n = cylinder(2.5, 2.5, 1.4, seg=32)
    parts.append((*transform(p, n, None, apex), C["dark"]))                   # mount adapter hub

    R_polar = rotmat("x", latitude - 90.0)
    head_base = apex + np.array([0, 0, 1.6])

    # Hour angle of the pointing, normalized to (-180, 180] (positive = west)
    H = 0.0
    if ra_hours is not None and lst_hours is not None:
        H = ((lst_hours - ra_hours) % 24.0) * 15.0
        if H > 180.0:
            H -= 360.0
    dec = dec_degrees if dec_degrees is not None else 90.0
    side = -1.0 if str(pier_side) == "pier_west" else 1.0

    # GEM geometry: one sky pointing has two mechanical solutions —
    #   pier east: body angle = -H  (dec sense +1)
    #   pier west: body angle = 180 - H  (dec sense -1)
    # and, like a real mount, the body angle is LIMITED: at most LIM_OVER of
    # meridian overlap and LIM_SWING of total swing, so inconsistent inputs
    # can never render a physically unreachable/colliding pose.
    LIM_OVER, LIM_SWING = 25.0, 115.0
    u = -H
    if side > 0:
        u = max(min(u, LIM_OVER), -LIM_SWING)
    else:
        u = min(max(u, -LIM_OVER), LIM_SWING)
    ha_deg = u if side > 0 else u + 180.0
    if ha_override is not None:
        ha_deg = float(ha_override)
    elif dec >= 88.5:
        ha_deg = float(os.environ.get("MV_HA", "270"))
    R_ha = rotmat("z", ha_deg)

    # Kinematic chain — exactly two moving joints:
    #   RA axis: latitude base (static) <-> square body. The whole body, with the
    #     Dec gearbox on top, turns about the polar axis (+Z) by the hour angle.
    #   Dec axis: square body <-> telescope. The saddle + scope + camera form one
    #     rigid unit turning about the Dec axis (the X line at z=DECZ), and that
    #     unit also follows the body's RA rotation.
    DECZ = 15.3
    off = np.array([0.0, 0.0, DECZ])
    R_dec = rotmat("x", side * (90.0 - dec))

    def place(P, N, col, grp):
        if grp == "dec":
            P = (P - off).astype("f4")
            P, N = transform(P, N, R_dec)
            P = (P + off).astype("f4")
        if grp in ("dec", "ra"):
            P, N = transform(P, N, R_ha)
        parts.append((*transform(P, N, R_polar, head_base), col))

    for P, N, col, grp in build_mount(C):
        place(P, N, col, grp)

    # scope + camera: one rigid OTA. Rolled -90 about its optical axis so its
    # underside dovetail faces -X (inboard), then dropped onto the saddle plate
    # at x=9.9 — the OTA centerline lands ON the Dec axis line (y=0), so at the
    # home pose it stands directly above the body, like the real AM5.
    R_roll = rotmat("z", -90.0)
    OTA_T = np.array([17.1, 0.0, 16.3])
    optics = list(build_scope(C))
    for P, N, col in build_camera(C):
        optics.append((*transform(P, N, None, (0, 0, -32.5)), col))
    for P, N, col in optics:
        P1, N1 = transform(P, N, R_roll, OTA_T)
        place(P1, N1, col, "dec")

    p1 = np.asarray(OTA_T, "f4") - off
    p1 = (R_dec @ p1) + off
    scope_center = (R_polar @ (R_ha @ p1)) + head_base
    return parts, scope_center


def _align_z(target):
    z = np.array([0, 0, 1.0])
    t = np.asarray(target, "f4")
    t = t / np.linalg.norm(t)
    v = np.cross(z, t)
    c = float(np.dot(z, t))
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3, dtype="f4") if c > 0 else rotmat("x", 180)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]], "f4")
    return (np.eye(3) + vx + vx @ vx * (1.0 / (1.0 + c))).astype("f4")


def _perspective(fovy, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fovy) / 2.0)
    return np.array([[f / aspect, 0, 0, 0], [0, f, 0, 0],
                     [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
                     [0, 0, -1, 0]], dtype="f4")


def _look_at(eye, center, up):
    eye = np.asarray(eye, "f4")
    center = np.asarray(center, "f4")
    up = np.asarray(up, "f4")
    F = center - eye
    F = F / np.linalg.norm(F)
    R = np.cross(F, up)
    R = R / np.linalg.norm(R)
    U = np.cross(R, F)
    M = np.eye(4, dtype="f4")
    M[0, :3] = R
    M[1, :3] = U
    M[2, :3] = -F
    M[0, 3] = -float(R @ eye)
    M[1, 3] = -float(U @ eye)
    M[2, 3] = float(F @ eye)
    return M


VERT = """
#version 330
in vec3 in_pos; in vec3 in_norm; in vec3 in_col;
uniform mat4 u_mvp;
out vec3 v_norm; out vec3 v_col; out vec3 v_pos;
void main(){ v_norm=in_norm; v_col=in_col; v_pos=in_pos; gl_Position=u_mvp*vec4(in_pos,1.0); }
"""
FRAG = """
#version 330
in vec3 v_norm; in vec3 v_col; in vec3 v_pos;
uniform vec3 u_eye; uniform vec3 u_l1; uniform vec3 u_l2;
out vec4 f_col;
void main(){
  vec3 N=normalize(v_norm);
  vec3 V=normalize(u_eye - v_pos);
  if(dot(N,V)<0.0) N=-N;
  vec3 L1=normalize(u_l1); vec3 L2=normalize(u_l2);
  float d1=max(dot(N,L1),0.0);
  float d2=max(dot(N,L2),0.0);
  vec3 H=normalize(L1+V);
  float spec=pow(max(dot(N,H),0.0),48.0)*0.45;
  vec3 c=v_col*(0.20 + 0.85*d1 + 0.30*d2) + spec*vec3(1.0);
  float rim=pow(1.0-max(dot(N,V),0.0),3.0)*0.18;
  c += rim*vec3(0.35,0.5,0.65);
  c=pow(clamp(c,0.0,1.0), vec3(0.86));
  f_col=vec4(c,1.0);
}
"""


SKY_VERT = """
#version 330
in vec3 in_pos; in vec4 in_col;
uniform mat4 u_mvp;
out vec4 v_col;
void main(){ v_col=in_col; gl_Position=u_mvp*vec4(in_pos,1.0); }
"""
SKY_FRAG = """
#version 330
in vec4 v_col; out vec4 f_col;
void main(){ f_col=v_col; }
"""
PT_VERT = """
#version 330
in vec3 in_pos; in float in_size; in vec4 in_col;
uniform mat4 u_mvp;
out vec4 v_col;
void main(){ v_col=in_col; gl_Position=u_mvp*vec4(in_pos,1.0); gl_PointSize=in_size; }
"""
PT_FRAG = """
#version 330
in vec4 v_col; out vec4 f_col;
void main(){
  vec2 d = gl_PointCoord - vec2(0.5);
  float r = length(d);
  if (r > 0.5) discard;
  f_col = vec4(v_col.rgb, v_col.a * smoothstep(0.5, 0.30, r));
}
"""


def render_png(parts, size=560, bg=(0.027, 0.035, 0.035), view_az=None, view_el=None,
               sky_lines=None, sky_points=None, labels=None, frame=None, fov=35.0):
    import moderngl
    ctx = moderngl.create_standalone_context()
    P = np.concatenate([p[0] for p in parts])
    N = np.concatenate([p[1] for p in parts])
    Cv = np.concatenate([np.broadcast_to(_arr(p[2]), (len(p[0]), 3)) for p in parts])
    data = np.hstack([P, N, Cv]).astype("f4")

    prog = ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
    vbo = ctx.buffer(data.tobytes())
    vao = ctx.vertex_array(prog, [(vbo, "3f 3f 3f", "in_pos", "in_norm", "in_col")])

    if frame is not None:
        center = np.asarray(frame[0], "f4")
        radius = float(frame[1])
        dist = radius * float(frame[2])
    else:
        lo = P.min(axis=0)
        hi = P.max(axis=0)
        center = (lo + hi) / 2.0
        radius = float(np.linalg.norm(hi - lo)) / 2.0
        dist = radius * 2.6
    if view_az is None:
        view_az = float(os.environ.get("MV_AZ", "-90"))
    if view_el is None:
        view_el = float(os.environ.get("MV_EL", "8"))
    az, el = math.radians(view_az), math.radians(max(-10.0, min(85.0, view_el)))
    eye = center + dist * np.array([math.cos(el) * math.sin(az), -math.cos(el) * math.cos(az), math.sin(el)])
    mvp = _perspective(max(10.0, min(80.0, fov)), 1.0, 0.5, dist * 4) @ _look_at(eye, center, (0, 0, 1))
    mvp_bytes = np.ascontiguousarray(mvp.T).tobytes()
    prog["u_mvp"].write(mvp_bytes)
    prog["u_eye"].value = tuple(float(x) for x in eye)
    prog["u_l1"].value = (-0.5, -0.55, 0.8)
    prog["u_l2"].value = (0.7, 0.3, 0.2)

    samples = 8
    cbo = ctx.renderbuffer((size, size), samples=samples)
    dbo = ctx.depth_renderbuffer((size, size), samples=samples)
    msaa = ctx.framebuffer(color_attachments=[cbo], depth_attachment=dbo)
    msaa.use()
    ctx.enable(moderngl.DEPTH_TEST)
    ctx.clear(*bg, 1.0)
    vao.render(moderngl.TRIANGLES)

    ctx.enable(moderngl.BLEND)
    if sky_lines:
        lprog = ctx.program(vertex_shader=SKY_VERT, fragment_shader=SKY_FRAG)
        seg = []
        cols = []
        for pts, rgba in sky_lines:
            pts = np.asarray(pts, "f4")
            if len(pts) < 2:
                continue
            inter = np.empty((2 * (len(pts) - 1), 3), "f4")
            inter[0::2] = pts[:-1]
            inter[1::2] = pts[1:]
            seg.append(inter)
            cols.append(np.tile(np.asarray(rgba, "f4"), (len(inter), 1)))
        if seg:
            LP = np.concatenate(seg)
            LC = np.concatenate(cols)
            lvbo = ctx.buffer(np.hstack([LP, LC]).astype("f4").tobytes())
            lvao = ctx.vertex_array(lprog, [(lvbo, "3f 4f", "in_pos", "in_col")])
            lprog["u_mvp"].write(mvp_bytes)
            lvao.render(moderngl.LINES)
    if sky_points is not None and len(sky_points[0]):
        pprog = ctx.program(vertex_shader=PT_VERT, fragment_shader=PT_FRAG)
        ctx.enable(moderngl.PROGRAM_POINT_SIZE)
        pp, ps, pc = sky_points
        pdata = np.hstack([np.asarray(pp, "f4"), np.asarray(ps, "f4").reshape(-1, 1),
                           np.asarray(pc, "f4")]).astype("f4")
        pvbo = ctx.buffer(pdata.tobytes())
        pvao = ctx.vertex_array(pprog, [(pvbo, "3f 1f 4f", "in_pos", "in_size", "in_col")])
        pprog["u_mvp"].write(mvp_bytes)
        pvao.render(moderngl.POINTS)

    out = ctx.simple_framebuffer((size, size))
    ctx.copy_framebuffer(out, msaa)
    raw = out.read(components=3)

    from PIL import Image
    img = Image.frombytes("RGB", (size, size), raw).transpose(Image.FLIP_TOP_BOTTOM)
    if labels:
        from PIL import ImageDraw

        from .sky_render import _font
        dr = ImageDraw.Draw(img)
        for pos, text, rgb, px in labels:
            v = mvp @ np.array([pos[0], pos[1], pos[2], 1.0], "f4")
            if v[3] <= 0:
                continue
            ndc = v[:3] / v[3]
            if abs(ndc[0]) > 1.05 or abs(ndc[1]) > 1.05:
                continue
            x = float((ndc[0] * 0.5 + 0.5) * size)
            y = float((1.0 - (ndc[1] * 0.5 + 0.5)) * size)
            dr.text((x, y), text, fill=tuple(int(c) for c in rgb), font=_font(px), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    ctx.release()
    return buf.getvalue()


SKY_RADIUS = 110.0
GROUND_COLOR = (0.036, 0.048, 0.042)


def build_ground():
    """Dark ground disk with faint surveyor rings/radials. Returns (tris, lines)."""
    tris = []
    p, n = disk(SKY_RADIUS, seg=96, z=-0.06, up=1.0)
    tris.append((p, n, GROUND_COLOR))
    lines = []
    for r in (SKY_RADIUS * 0.25, SKY_RADIUS * 0.5, SKY_RADIUS * 0.75):
        pts = [np.array([r * math.cos(math.radians(a)), r * math.sin(math.radians(a)), 0.05], "f4")
               for a in range(0, 362, 3)]
        lines.append((np.array(pts, "f4"), (0.16, 0.20, 0.17, 0.45)))
    for a in range(0, 360, 45):
        ca, sa = math.cos(math.radians(a)), math.sin(math.radians(a))
        pts = [np.array([10 * ca, 10 * sa, 0.05], "f4"),
               np.array([SKY_RADIUS * 0.995 * ca, SKY_RADIUS * 0.995 * sa, 0.05], "f4")]
        lines.append((np.array(pts, "f4"), (0.14, 0.18, 0.15, 0.4)))
    return tris, lines


def build_sky(lst_hours, lat, ra_hours=None, dec_degrees=None,
              target_ra=None, target_dec=None, eqgrid=True, altgrid=False,
              scope_center=None, size=560):
    """Celestial dome centered on the rig's ground position: equatorial /
    alt-az grids, mag<=5 stars, the milky way, the sun, the goto target and
    the live pointing ray. Returns (lines, points, labels) for render_png."""
    from .sky_render import _alt_az, _sun_ra_dec
    from .sky_stars import NAMED, STARS
    R = SKY_RADIUS
    lst = lst_hours if lst_hours is not None else 0.0
    s = size / 760.0

    def dirv(alt, azd):
        a = math.radians(azd)
        e = math.radians(alt)
        return np.array([math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)], "f4")

    def eq_pos(ra_h, dec_d):
        return _alt_az((lst - ra_h) * 15.0, dec_d, lat)

    lines = []
    labels = []

    def eq_polyline(samples, rgba):
        run = []
        for ra_h, dec_d in samples:
            alt, azd = eq_pos(ra_h, dec_d)
            if alt > -0.2:
                run.append(dirv(alt, azd) * R)
            else:
                if len(run) > 1:
                    lines.append((np.array(run, "f4"), rgba))
                run = []
        if len(run) > 1:
            lines.append((np.array(run, "f4"), rgba))

    if eqgrid:
        for dec_c in (-30, 0, 30, 60):
            samp = [(h / 4.0, float(dec_c)) for h in range(0, 97)]
            eq_polyline(samp, (0.25, 0.36, 0.37, 0.85) if dec_c == 0 else (0.16, 0.23, 0.24, 0.55))
        for ra_c in range(0, 24, 2):
            samp = [(float(ra_c), float(d)) for d in range(-60, 89)]
            eq_polyline(samp, (0.16, 0.23, 0.24, 0.5))
    if altgrid:
        for alt_c in (30, 60):
            pts = [dirv(alt_c, a) * R for a in range(0, 363, 3)]
            lines.append((np.array(pts, "f4"), (0.36, 0.31, 0.20, 0.6)))
        for az_c in range(0, 360, 45):
            pts = [dirv(float(a), float(az_c)) * R for a in range(0, 87, 3)]
            lines.append((np.array(pts, "f4"), (0.36, 0.31, 0.20, 0.5)))

    pts = [dirv(0.0, a) * R for a in range(0, 362, 2)]
    lines.append((np.array(pts, "f4"), (0.36, 0.44, 0.43, 0.95)))  # horizon ring

    pos = []
    sizes = []
    cols = []
    for ra_h, dec_d, mag in STARS:
        alt, azd = eq_pos(ra_h, dec_d)
        if alt <= 0:
            continue
        pos.append(dirv(alt, azd) * R * 0.995)
        sizes.append(max(1.2, 4.8 - 0.78 * mag) * s)
        cols.append((0.88, 0.92, 0.92, max(0.28, min(1.0, 1.04 - 0.13 * mag))))

    # milky way: deterministic scatter along the galactic equator (J2000 matrix)
    M = ((-0.0548755604, 0.4941094279, -0.8676661490),
         (-0.8734370902, -0.4448296300, -0.1980763734),
         (-0.4838350155, 0.7469822445, 0.4559837762))
    for i in range(2600):
        f1 = math.modf(math.sin(i * 12.9898) * 43758.5453)[0]
        f2 = math.modf(math.sin(i * 78.233) * 12578.1459)[0]
        gl = (i / 2600.0) * 360.0 + f1 * 3.0
        w = 7.0 + 5.0 * max(0.0, math.cos(math.radians(gl)))
        gb = (abs(f2) * 2.0 - 1.0) * w
        lr, br = math.radians(gl), math.radians(gb)
        vg = (math.cos(br) * math.cos(lr), math.cos(br) * math.sin(lr), math.sin(br))
        vx = M[0][0] * vg[0] + M[0][1] * vg[1] + M[0][2] * vg[2]
        vy = M[1][0] * vg[0] + M[1][1] * vg[1] + M[1][2] * vg[2]
        vz = M[2][0] * vg[0] + M[2][1] * vg[1] + M[2][2] * vg[2]
        ra_h = (math.degrees(math.atan2(vy, vx)) % 360.0) / 15.0
        dec_d = math.degrees(math.asin(max(-1.0, min(1.0, vz))))
        alt, azd = eq_pos(ra_h, dec_d)
        if alt <= 0:
            continue
        pos.append(dirv(alt, azd) * R * 0.998)
        sizes.append((1.6 + 1.4 * abs(f1)) * s)
        cols.append((0.56, 0.63, 0.74, 0.17))

    sra, sdec = _sun_ra_dec()
    alt, azd = eq_pos(sra, sdec)
    if alt > 0:
        p = dirv(alt, azd) * R * 0.99
        pos.append(p)
        sizes.append(10.0 * s)
        cols.append((0.91, 0.70, 0.22, 0.95))
        labels.append((p * 1.05, "日", (232, 179, 57), max(11, int(13 * s))))

    # Pointing/target markers are anchored to the SCOPE line (not the ground
    # origin): with a finite dome the ~50cm scope height causes ~20 deg of
    # parallax, which would draw the ray visibly non-parallel to the tube.
    anchor = np.asarray(scope_center, "f4") if scope_center is not None else np.array([0, 0, 40.0], "f4")

    def sphere_hit(start, d):
        b = float(np.dot(start, d))
        c = float(np.dot(start, start)) - (R * 0.992) ** 2
        t = -b + math.sqrt(max(b * b - c, 1e-6))
        return start + d * t

    def ring_at(pt, nrm, ang_deg, rgba):
        cd = np.asarray(nrm, "f4")
        cd = cd / np.linalg.norm(cd)
        ref = np.array([0, 0, 1.0], "f4") if abs(cd[2]) < 0.95 else np.array([1.0, 0, 0], "f4")
        u = np.cross(cd, ref)
        u = u / np.linalg.norm(u)
        v = np.cross(cd, u)
        rr = math.radians(ang_deg) * R
        ring_pts = []
        for k in range(38):
            t = k / 37.0 * 2 * math.pi
            ring_pts.append(pt + (u * math.cos(t) + v * math.sin(t)) * rr)
        lines.append((np.array(ring_pts, "f4"), rgba))

    if target_ra is not None and target_dec is not None:
        alt, azd = eq_pos(float(target_ra), float(target_dec))
        if alt > 0:
            d = dirv(alt, azd)
            ring_at(sphere_hit(anchor, d), d, 2.6, (0.22, 0.85, 0.54, 0.95))

    if ra_hours is not None and dec_degrees is not None:
        alt, azd = eq_pos(float(ra_hours), float(dec_degrees))
        if alt > -5:
            d = dirv(alt, azd)
            hit = sphere_hit(anchor, d)
            ring_at(hit, d, 2.0, (0.89, 0.29, 0.25, 0.95))
            lines.append((np.array([anchor, hit], "f4"), (0.89, 0.29, 0.25, 0.55)))

    for azd, name in ((0, "北"), (90, "东"), (180, "南"), (270, "西")):
        labels.append((dirv(1.2, azd) * R * 1.03, name, (120, 138, 136), max(12, int(15 * s))))
    for ra_h, dec_d, name in NAMED:
        alt, azd = eq_pos(ra_h, dec_d)
        if alt > 3:
            labels.append((dirv(alt, azd) * R * 1.012, name, (95, 110, 108), max(10, int(11 * s))))

    if pos:
        points = (np.array(pos, "f4"), np.array(sizes, "f4"), np.array(cols, "f4"))
    else:
        points = (np.zeros((0, 3), "f4"), np.zeros((0,), "f4"), np.zeros((0, 4), "f4"))
    return lines, points, labels


def render_mount_png(ra_hours, dec_degrees, lst_hours=None, pier_side="pier_east", latitude=40.0, size=560,
                     view_az=None, view_el=None, ha_override=None,
                     sky=False, eqgrid=True, altgrid=False, target_ra=None, target_dec=None, fov=35.0,
                     ground=True):
    parts, scope_center = build_scene(ra_hours, dec_degrees, lst_hours, pier_side, latitude, ha_override=ha_override)
    sky_lines = sky_points = labels = frame = None
    if sky:
        sky_lines, sky_points, labels = build_sky(
            lst_hours, latitude, ra_hours, dec_degrees, target_ra, target_dec,
            bool(eqgrid), bool(altgrid), scope_center, size)
        frame = ((0.0, 0.0, SKY_RADIUS * 0.40), SKY_RADIUS * 1.02, 3.1)
    elif ground:
        P0 = np.concatenate([p[0] for p in parts])
        lo = P0.min(axis=0)
        hi = P0.max(axis=0)
        frame = (((lo + hi) / 2.0), float(np.linalg.norm(hi - lo)) / 2.0, 2.6)
    if ground:
        g_tris, g_lines = build_ground()
        parts = list(parts) + g_tris
        sky_lines = (list(sky_lines) if sky_lines else []) + g_lines
    return render_png(parts, size=size, view_az=view_az, view_el=view_el,
                      sky_lines=sky_lines, sky_points=sky_points, labels=labels, frame=frame, fov=fov)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ra", type=float, default=None)
    ap.add_argument("--dec", type=float, default=90.0)
    ap.add_argument("--lst", type=float, default=None)
    ap.add_argument("--pier", default="pier_east")
    ap.add_argument("--lat", type=float, default=40.0)
    ap.add_argument("--size", type=int, default=560)
    ap.add_argument("--az", type=float, default=None)
    ap.add_argument("--el", type=float, default=None)
    ap.add_argument("--ha", type=float, default=None)
    ap.add_argument("--sky", type=int, default=0)
    ap.add_argument("--eqgrid", type=int, default=1)
    ap.add_argument("--altgrid", type=int, default=0)
    ap.add_argument("--tra", type=float, default=None)
    ap.add_argument("--tdec", type=float, default=None)
    ap.add_argument("--fov", type=float, default=35.0)
    ap.add_argument("--ground", type=int, default=1)
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    if a.serve:
        return serve_loop()
    png = render_mount_png(a.ra, a.dec, a.lst, a.pier, a.lat, a.size,
                           view_az=a.az, view_el=a.el, ha_override=a.ha,
                           sky=bool(a.sky), eqgrid=bool(a.eqgrid), altgrid=bool(a.altgrid),
                           target_ra=a.tra, target_dec=a.tdec, fov=a.fov, ground=bool(a.ground))
    if a.out:
        with open(a.out, "wb") as fh:
            fh.write(png)
    else:
        sys.stdout.buffer.write(png)
    return 0


# --------------------------------------------------------------------------- #
#  persistent GPU render worker: keeps the GL context + geometry caches warm  #
#  so interactive orbiting costs ~20-40 ms/frame instead of ~500 ms           #
# --------------------------------------------------------------------------- #
_WORKER_LOCK = threading.Lock()
_WORKER: dict = {"proc": None}


def _worker_render(params: dict, root: str) -> bytes:
    import json as _json
    with _WORKER_LOCK:
        proc = _WORKER.get("proc")
        if proc is None or proc.poll() is not None:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(root) / "src")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            proc = subprocess.Popen(
                [sys.executable, "-B", "-m", "asiairbridge.mount_render", "--serve"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=str(root), env=env, bufsize=0)
            _WORKER["proc"] = proc
        try:
            proc.stdin.write((_json.dumps(params) + "\n").encode("utf-8"))
            proc.stdin.flush()
            header = proc.stdout.readline()
            tag, _, num = header.strip().partition(b" ")
            n = int(num)
            data = b""
            while len(data) < n:
                chunk = proc.stdout.read(n - len(data))
                if not chunk:
                    raise RuntimeError("render worker closed the pipe")
                data += chunk
            if tag != b"OK":
                raise RuntimeError(data.decode("utf-8", "replace"))
            return data
        except Exception:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            _WORKER["proc"] = None
            raise


def serve_loop() -> int:
    import json as _json

    import moderngl
    ctx = moderngl.create_standalone_context()
    progs = {
        "tri": ctx.program(vertex_shader=VERT, fragment_shader=FRAG),
        "line": ctx.program(vertex_shader=SKY_VERT, fragment_shader=SKY_FRAG),
        "pt": ctx.program(vertex_shader=PT_VERT, fragment_shader=PT_FRAG),
    }
    fbos: dict = {}
    scene_cache: dict = {}
    sky_cache: dict = {}
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        line = stdin.readline()
        if not line:
            return 0
        try:
            png = _serve_frame(ctx, progs, fbos, scene_cache, sky_cache, _json.loads(line))
            stdout.write(b"OK %d\n" % len(png))
            stdout.write(png)
        except Exception as exc:  # noqa: BLE001
            msg = repr(exc).encode("utf-8", "replace")[:400]
            stdout.write(b"ERR %d\n" % len(msg))
            stdout.write(msg)
        stdout.flush()


def _serve_frame(ctx, progs, fbos, scene_cache, sky_cache, q):
    import moderngl

    def f(name, default=None):
        v = q.get(name)
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    ra = f("ra")
    dec = f("dec", 90.0)
    lst = f("lst")
    lat = f("lat", 40.0)
    pier = str(q.get("pier") or "pier_east")
    size = int(f("size", 560.0))
    ha = f("ha")
    sky = bool(int(f("sky", 0.0)))
    eqg = bool(int(f("eqgrid", 1.0)))
    alg = bool(int(f("altgrid", 0.0)))
    tra = f("tra")
    tdec = f("tdec")
    view_az = f("az")
    view_el = f("el")
    fov = f("fov", 35.0)
    gnd = bool(int(f("ground", 1.0)))

    pkey = (ra, dec, lst, pier, lat, ha)
    ent = scene_cache.get(pkey)
    if ent is None:
        parts, scope_center = build_scene(ra, dec, lst, pier, lat, ha_override=ha)
        P = np.concatenate([p[0] for p in parts])
        N = np.concatenate([p[1] for p in parts])
        Cv = np.concatenate([np.broadcast_to(_arr(p[2]), (len(p[0]), 3)) for p in parts])
        data = np.hstack([P, N, Cv]).astype("f4").tobytes()
        lo = P.min(axis=0)
        hi = P.max(axis=0)
        ent = (data, scope_center, ((lo + hi) / 2.0, float(np.linalg.norm(hi - lo)) / 2.0))
        if len(scene_cache) > 8:
            scene_cache.clear()
        scene_cache[pkey] = ent
    tri_data, scope_center, bbox = ent

    gent = fbos.get("_ground")
    if gent is None:
        g_tris, g_lines = build_ground()
        gp = np.concatenate([t[0] for t in g_tris])
        gn = np.concatenate([t[1] for t in g_tris])
        gc = np.concatenate([np.broadcast_to(_arr(t[2]), (len(t[0]), 3)) for t in g_tris])
        g_tri_bytes = np.hstack([gp, gn, gc]).astype("f4").tobytes()
        seg = []
        cols = []
        for pts0, rgba in g_lines:
            inter = np.empty((2 * (len(pts0) - 1), 3), "f4")
            inter[0::2] = pts0[:-1]
            inter[1::2] = pts0[1:]
            seg.append(inter)
            cols.append(np.tile(np.asarray(rgba, "f4"), (len(inter), 1)))
        g_line_bytes = np.hstack([np.concatenate(seg), np.concatenate(cols)]).astype("f4").tobytes()
        gent = (g_tri_bytes, g_line_bytes)
        fbos["_ground"] = gent
    if gnd:
        tri_data = tri_data + gent[0]

    lines_data = pts_data = labels = None
    if sky:
        from datetime import datetime as _dt
        skey = (pkey, eqg, alg, tra, tdec, size, _dt.now().strftime("%Y%m%d%H"))
        sent = sky_cache.get(skey)
        if sent is None:
            sky_lines, sky_points, labels0 = build_sky(lst, lat, ra, dec, tra, tdec, eqg, alg, scope_center, size)
            seg = []
            cols = []
            for pts0, rgba in sky_lines:
                pts0 = np.asarray(pts0, "f4")
                if len(pts0) < 2:
                    continue
                inter = np.empty((2 * (len(pts0) - 1), 3), "f4")
                inter[0::2] = pts0[:-1]
                inter[1::2] = pts0[1:]
                seg.append(inter)
                cols.append(np.tile(np.asarray(rgba, "f4"), (len(inter), 1)))
            ld = np.hstack([np.concatenate(seg), np.concatenate(cols)]).astype("f4").tobytes() if seg else None
            pp, ps, pc = sky_points
            pd = np.hstack([np.asarray(pp, "f4"), np.asarray(ps, "f4").reshape(-1, 1),
                            np.asarray(pc, "f4")]).astype("f4").tobytes() if len(pp) else None
            sent = (ld, pd, labels0)
            if len(sky_cache) > 6:
                sky_cache.clear()
            sky_cache[skey] = sent
        lines_data, pts_data, labels = sent

    if sky:
        center = np.array([0.0, 0.0, SKY_RADIUS * 0.40], "f4")
        radius = SKY_RADIUS * 1.02
        dist = radius * 3.1
    else:
        center, radius = bbox
        dist = radius * 2.6
    if gnd:
        lines_data = (lines_data or b"") + gent[1]
    if view_az is None:
        view_az = float(os.environ.get("MV_AZ", "-90"))
    if view_el is None:
        view_el = float(os.environ.get("MV_EL", "8"))
    azr = math.radians(view_az)
    elr = math.radians(max(-10.0, min(85.0, view_el)))
    eye = center + dist * np.array([math.cos(elr) * math.sin(azr), -math.cos(elr) * math.cos(azr), math.sin(elr)])
    mvp = _perspective(max(10.0, min(80.0, fov or 35.0)), 1.0, 0.5, dist * 4) @ _look_at(eye, center, (0, 0, 1))
    mvpb = np.ascontiguousarray(mvp.T).tobytes()

    fb = fbos.get(size)
    if fb is None:
        cbo = ctx.renderbuffer((size, size), samples=8)
        dbo = ctx.depth_renderbuffer((size, size), samples=8)
        fb = (ctx.framebuffer(color_attachments=[cbo], depth_attachment=dbo), ctx.simple_framebuffer((size, size)))
        fbos[size] = fb
    msaa, outfb = fb
    msaa.use()
    ctx.enable(moderngl.DEPTH_TEST)
    ctx.clear(0.027, 0.035, 0.035, 1.0)
    tp = progs["tri"]
    tp["u_mvp"].write(mvpb)
    tp["u_eye"].value = tuple(float(x) for x in eye)
    tp["u_l1"].value = (-0.5, -0.55, 0.8)
    tp["u_l2"].value = (0.7, 0.3, 0.2)
    vbo = ctx.buffer(tri_data)
    vao = ctx.vertex_array(tp, [(vbo, "3f 3f 3f", "in_pos", "in_norm", "in_col")])
    vao.render(moderngl.TRIANGLES)
    vao.release()
    vbo.release()
    ctx.enable(moderngl.BLEND)
    if lines_data:
        lp = progs["line"]
        lp["u_mvp"].write(mvpb)
        lvbo = ctx.buffer(lines_data)
        lvao = ctx.vertex_array(lp, [(lvbo, "3f 4f", "in_pos", "in_col")])
        lvao.render(moderngl.LINES)
        lvao.release()
        lvbo.release()
    if pts_data:
        ppr = progs["pt"]
        ppr["u_mvp"].write(mvpb)
        ctx.enable(moderngl.PROGRAM_POINT_SIZE)
        pvbo = ctx.buffer(pts_data)
        pvao = ctx.vertex_array(ppr, [(pvbo, "3f 1f 4f", "in_pos", "in_size", "in_col")])
        pvao.render(moderngl.POINTS)
        pvao.release()
        pvbo.release()
    ctx.copy_framebuffer(outfb, msaa)
    raw = outfb.read(components=3)

    from PIL import Image
    img = Image.frombytes("RGB", (size, size), raw).transpose(Image.FLIP_TOP_BOTTOM)
    if labels:
        from PIL import ImageDraw

        from .sky_render import _font
        dr = ImageDraw.Draw(img)
        for pos, text, rgb, px in labels:
            v = mvp @ np.array([pos[0], pos[1], pos[2], 1.0], "f4")
            if v[3] <= 0:
                continue
            ndc = v[:3] / v[3]
            if abs(ndc[0]) > 1.05 or abs(ndc[1]) > 1.05:
                continue
            x = float((ndc[0] * 0.5 + 0.5) * size)
            y = float((1.0 - (ndc[1] * 0.5 + 0.5)) * size)
            dr.text((x, y), text, fill=tuple(int(c) for c in rgb), font=_font(px), anchor="mm")
    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
