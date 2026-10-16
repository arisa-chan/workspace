"""3D visualisation of the connection (plates, bolts, load) with matplotlib."""
from __future__ import annotations

import math
from typing import Optional, Tuple

from matplotlib.colors import to_rgba

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

from .model import ConnectionInput

COL_PLATE_A = "#4c72b0"
COL_PLATE_B = "#dd8452"
COL_BOLT = "#55a868"
COL_LOAD = "#c44e52"


def _box(x0, x1, y0, y1, z0, z1):
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]])
    faces = [[0, 1, 2, 3], [4, 5, 6, 7], [0, 1, 5, 4],
             [2, 3, 7, 6], [1, 2, 6, 5], [0, 3, 7, 4]]
    return [v[f] for f in faces]


def _cylinder_y(x, z, y0, y1, r, n=16):
    th = np.linspace(0, 2 * math.pi, n, endpoint=False)
    ring0 = np.stack([x + r * np.cos(th), np.full(n, y0), z + r * np.sin(th)], axis=1)
    ring1 = np.stack([x + r * np.cos(th), np.full(n, y1), z + r * np.sin(th)], axis=1)
    quads = []
    for i in range(n):
        j = (i + 1) % n
        quads.append([ring0[i], ring0[j], ring1[j], ring1[i]])
    quads.append(ring0[::-1])
    quads.append(ring1)
    return quads


class Connection3DView(FigureCanvasQTAgg):
    def __init__(self, parent=None):
        self.fig = Figure(figsize=(8, 6), dpi=100)
        super().__init__(self.fig)
        self.setParent(parent)
        self.ax = self.fig.add_subplot(projection="3d")

    def draw_connection(self, ci: ConnectionInput, ic=None,
                        bolt_forces=None):
        ax = self.ax
        ax.clear()
        L = dict(ta=ci.plate_a.thickness, tb=ci.plate_b.thickness)
        ya0, ya1 = -L['ta'] / 2, L['ta'] / 2
        yb0, yb1 = ya1, ya1 + L['tb']

        # plates ----------------------------------------------------------
        for k, (plate, y0, y1, col) in enumerate((
                (ci.plate_a, ya0, ya1, COL_PLATE_A),
                (ci.plate_b, yb0, yb1, COL_PLATE_B))):
            ob = plate.outline()
            xs = [p[0] for p in ob]
            zs = [p[1] for p in ob]
            pc = Poly3DCollection(
                _box(min(xs), max(xs), y0, y1, min(zs), max(zs)),
                facecolor=to_rgba(col, 0.92 if col == COL_PLATE_A else 0.42),
                edgecolor="k", linewidths=0.4)
            # explicit painter order: support plate behind the bolts,
            # translucent loaded plate in front of them
            pc.set_sort_zpos(-0.5 if k == 0 else 0.5)
            ax.add_collection3d(pc)

        # bolts -----------------------------------------------------------
        for (bx, bz) in ci.bolts_xy:
            d = ci.bolt_diameter
            pc = Poly3DCollection(
                _cylinder_y(bx, bz, ya0 - 0.65 * d, yb1 + 0.8 * d, d / 2),
                facecolor=COL_BOLT, edgecolor="#2f6b3f", linewidths=0.3)
            pc.set_sort_zpos(0.0)
            ax.add_collection3d(pc)
            # head + nut (hexagon-ish cylinders)
            for yy0, yy1 in ((ya0 - 0.65 * d - 0.001, ya0),
                             (yb1, yb1 + 0.8 * d + 0.001)):
                pc = Poly3DCollection(
                    _cylinder_y(bx, bz, yy0, yy1, 0.8 * d, n=6),
                    facecolor="#3d7a4c", edgecolor="#2f6b3f", linewidths=0.3)
                pc.set_sort_zpos(0.0)
                ax.add_collection3d(pc)

        # load arrow --------------------------------------------------------
        dx, dz = ci.load.direction
        h = ci.load.h_load if ci.load.h_load is not None else (
            ci.plate_b.cy + ci.plate_b.height / 2)
        p0 = np.array([ci.load.e, yb1 + 0.02, h])
        length = 0.35 * max(ci.plate_a.height, ci.plate_a.width)
        ax.quiver(p0[0], p0[1], p0[2], dx * length, 0, dz * length,
                  color=COL_LOAD, linewidth=2.5, arrow_length_ratio=0.15)
        ax.text(p0[0] + dx * length, p0[1], p0[2] + dz * length, "  P",
                color=COL_LOAD, fontsize=12, weight="bold")

        # bolt-group centroid + instantaneous centre -------------------------
        ymark = yb1 + 0.012
        cx, cy = ci.centroid()
        sc = ax.scatter([cx], [ymark], [cy], marker="o", color="k", s=25)
        try:
            sc.set_sort_zpos(1.0)
        except Exception:
            pass
        if ic is not None:
            sc2 = ax.scatter([ic[0]], [ymark], [ic[1]], marker="x",
                             color="m", s=60, linewidths=2.5)
            try:
                sc2.set_sort_zpos(1.0)
            except Exception:
                pass
            ax.text(ic[0], ymark, ic[1], "  ICR", color="m", fontsize=10)

        # bolt force arrows (in-plane) ---------------------------------------
        if bolt_forces is not None and len(bolt_forces):
            f = np.asarray(bolt_forces, dtype=float)
            scale = 0.25 * max(ci.plate_a.height, ci.plate_a.width) / max(
                1e-12, np.abs(f).max())
            qv = ax.quiver(bolts_x := [b[0] for b in ci.bolts_xy],
                           [ymark + 0.004] * len(f),
                           [b[1] for b in ci.bolts_xy],
                           [v[0] * scale for v in f], [0] * len(f),
                           [v[1] * scale for v in f],
                           color="#8172b3", linewidth=1.4, arrow_length_ratio=0.2)
            try:
                qv.set_sort_zpos(1.2)
            except Exception:
                pass

        # cosmetics ----------------------------------------------------------
        allx = [p[0] for p in ci.plate_a.outline()] + \
               [p[0] for p in ci.plate_b.outline()] + [ci.load.e]
        ally = [ya0 - 0.1, yb1 + 0.1]
        allz = [p[1] for p in ci.plate_a.outline()] + \
               [p[1] for p in ci.plate_b.outline()] + [h]
        rng = max(max(allx) - min(allx), max(allz) - min(allz)) * 1.15
        mx, mz = (max(allx) + min(allx)) / 2, (max(allz) + min(allz)) / 2
        ax.set_xlim(mx - rng / 2, mx + rng / 2)
        ax.set_ylim(-rng / 2, rng / 2)
        ax.set_zlim(mz - rng / 2, mz + rng / 2)
        try:
            ax.set_box_aspect((1, 1, 1))
        except Exception:
            pass
        ax.set_xlabel("X [m]")
        ax.set_ylabel("Y (bolt axis) [m]")
        ax.set_zlabel("Z [m]")
        ax.set_title(ci.title)
        ax.scatter([ci.load.e], [yb1], [h], marker="o", color=COL_LOAD, s=30)
        ax.view_init(elev=16, azim=65)
        self.fig.tight_layout()
        self.draw()
