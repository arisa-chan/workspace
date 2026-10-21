"""2D plan view of the bolt group with force vectors, ICR and load line."""
from __future__ import annotations

import numpy as np
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.figure import Figure
from matplotlib.patches import Circle
from matplotlib.collections import PolyCollection
from matplotlib.colors import Normalize
from matplotlib.cm import ScalarMappable

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


class FeMeshView(FigureCanvasQTAgg):
    """Visualisation of the OpenSees fallback mesh, stresses and bolt forces."""

    def __init__(self, parent=None):
        self.fig = Figure(figsize=(7, 6), dpi=100)
        super().__init__(self.fig)
        self.setParent(parent)
        self.ax = self.fig.add_subplot(111)
        self._cbar = None

    def draw_mesh(self, mesh=None, show_stress=True, show_forces=True,
                  title="FE fallback mesh"):
        ax = self.ax
        ax.clear()
        if self._cbar is not None:
            try:
                self._cbar.remove()
            except Exception:
                pass
            self._cbar = None
        if not mesh or not mesh.get("plates"):
            ax.text(0.5, 0.5, "Run the FE fallback (or press Preview mesh)\n"
                    "to display the mesh.", ha="center", va="center",
                    transform=ax.transAxes, fontsize=12, color="#666")
            ax.set_axis_off()
            self.draw()
            return
        ax.set_axis_on()
        all_stress = []
        for plate in mesh["plates"]:
            all_stress.extend(plate.get("stress") or [])
        vmax = max(all_stress) if all_stress else 0.0
        norm = Normalize(vmin=0.0, vmax=max(vmax, 1.0))
        cmap = "viridis"
        for k, plate in enumerate(mesh["plates"]):
            xy = {n[0]: (n[1], n[2]) for n in plate["nodes"]}
            polys = []
            vals = []
            for e, tags in enumerate(plate["elements"]):
                try:
                    polys.append([xy[t] for t in tags])
                    vals.append(plate["stress"][e] if e < len(
                        plate["stress"]) else 0.0)
                except KeyError:
                    continue
            if not polys:
                continue
            if show_stress and vmax > 0:
                pc = PolyCollection(polys, array=np.asarray(vals), cmap=cmap,
                                    norm=norm, edgecolors="#333333",
                                    linewidths=0.15,
                                    alpha=0.55 if k == 0 else 0.80)
            else:
                pc = PolyCollection(
                    polys, facecolors=("#4c72b0" if k == 0 else "#dd8452"),
                    edgecolors="#333333", linewidths=0.2,
                    alpha=0.35 if k == 0 else 0.55)
            ax.add_collection(pc)
        if show_stress and vmax > 0:
            sm = ScalarMappable(norm=norm, cmap=cmap)
            sm.set_array([])
            self._cbar = self.fig.colorbar(sm, ax=ax, fraction=0.046,
                                           pad=0.04)
            self._cbar.set_label("von Mises stress [Pa]")
        for (bx, bz) in mesh.get("bolts", []):
            ax.add_patch(Circle((bx, bz),
                                max(mesh.get("hole_radius", 0.005), 1e-4),
                                facecolor="#55a868", edgecolor="k",
                                lw=0.8, zorder=5))
        if show_forces and mesh.get("forces"):
            f = np.asarray(mesh["forces"], float)
            bolts = np.asarray(mesh.get("bolts", []), float)
            if len(f) == len(bolts) and len(f):
                span = max(np.ptp(bolts[:, 0]), np.ptp(bolts[:, 1]),
                           mesh.get("mesh_max", 0.01) * 5.0)
                scale = 0.30 * span / max(1e-12, np.abs(f).max())
                ax.quiver(bolts[:, 0], bolts[:, 1], f[:, 0] * scale,
                          f[:, 1] * scale, angles="xy", scale_units="xy",
                          scale=1, color="#c44e52", width=0.006,
                          zorder=6, label="bolt forces")
                ax.legend(loc="upper left", fontsize=8)
        ax.set_aspect("equal")
        ax.set_xlabel("x [m]")
        ax.set_ylabel("y [m]")
        ax.set_title(title + (" — stress contour" if show_stress else ""))
        ax.grid(alpha=0.2)
        self.fig.tight_layout()
        self.draw()
