"""2D plan view of the bolt group with force vectors, ICR and load line."""
from __future__ import annotations

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.patches import Circle

from .model import ConnectionInput


class Connection2DView(FigureCanvasQTAgg):
    def __init__(self, parent=None):
        self.fig = Figure(figsize=(6, 6), dpi=100)
        super().__init__(self.fig)
        self.setParent(parent)
        self.ax = self.fig.add_subplot(111)

    def draw_group(self, ci: ConnectionInput, ic=None, bolt_forces=None,
                   scale_unit=""):
        ax = self.ax
        ax.clear()
        bolts = np.asarray(ci.bolts_xy, float)
        for plate, col in ((ci.plate_a, "#4c72b0"), (ci.plate_b, "#dd8452")):
            ob = plate.outline()
            ax.fill([p[0] for p in ob] + [ob[0][0]],
                    [p[1] for p in ob] + [ob[0][1]],
                    facecolor=col, alpha=0.18, edgecolor=col, lw=1.2,
                    label=plate.name)
        r = ci.bolt_diameter / 2
        for (bx, by) in bolts:
            ax.add_patch(Circle((bx, by), r, facecolor="#55a868",
                                edgecolor="k", lw=0.8, zorder=3))
        cg = ci.centroid()
        ax.plot(*cg, "k+", ms=12, mew=2, label="centroid")
        if ic is not None:
            ax.plot(ic[0], ic[1], "mx", ms=11, mew=2.5, label="ICR (ultimate)")
        if bolt_forces is not None and len(bolt_forces):
            f = np.asarray(bolt_forces, float)
            s = 0.30 * max(ci.plate_a.height, ci.plate_a.width) / max(1e-12, np.abs(f).max())
            ax.quiver(bolts[:, 0], bolts[:, 1], f[:, 0] * s, f[:, 1] * s,
                      angles="xy", scale_units="xy", scale=1, color="#8172b3",
                      width=0.006, label=f"bolt forces {scale_unit}")
        # load line of action
        dx, dz = ci.load.direction
        h = ci.load.h_load if ci.load.h_load is not None else (
            ci.plate_b.cy + ci.plate_b.height / 2)
        p0 = np.array([ci.load.e, h])
        t = np.linspace(-0.35, 0.35, 2)
        ax.plot(p0[0] + dx * t * max(1.0, ci.plate_a.height),
                p0[1] + dz * t * max(1.0, ci.plate_a.height),
                "--", color="#c44e52", lw=1.4, label="load line of action")
        ax.annotate("", xy=p0 + np.array([dx, dz]) * 0.12 * ci.plate_a.height,
                    xytext=p0 - np.array([dx, dz]) * 0.12 * ci.plate_a.height,
                    arrowprops=dict(arrowstyle="-|>", color="#c44e52", lw=2.4))
        ax.set_aspect("equal")
        ax.grid(alpha=0.3)
        ax.legend(loc="upper left", fontsize=8)
        ax.set_xlabel("x [m]")
        ax.set_ylabel("y [m]")
        ax.set_title("Bolt group - plan view (connection plane)")
        self.fig.tight_layout()
        self.draw()
