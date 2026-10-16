# EccentricBolt — Eccentrically Loaded Bolted Steel Shear Connections
### PySide6 GUI + IDEA StatiCa Connection API + Crawford & Kulak (1971) verification

A desktop application that models, visualises (3-D) and analyses **eccentrically
loaded bolted steel shear connections**:

* **inputs** – bolt coordinates, plate dimensions & steel grades, bolt diameter
  & grade, load magnitude, angle from vertical and eccentricity;
* **3-D view** – plates and bolts drawn at their true sizes/positions
  (plus load vector, bolt-group centroid, instantaneous centre and bolt forces);
* **analysis 1** – built-in ultimate-strength solver implementing the
  **instantaneous-centre-of-rotation method of Crawford & Kulak (1971)**
  (*Eccentrically Loaded Bolted Connections*, J. Struct. Div. ASCE 97(ST3));
* **analysis 2** – **IDEA StatiCa Connection** (CBFEM) through the official
  **Connection REST API** Python SDK (`ideastatica_connection_api`): the GUI
  generates an **IOM (IDEA Open Model)** XML of the connection, imports it as a
  project, applies the eccentric load case, runs the CBFEM calculation and
  reads back bolt/plate check results, the `.ideaCon` project and a PDF report.

![GUI](docs/screenshot_main.png)

---

## 1. Quick start

```bash
pip install -r requirements.txt
python main.py                 # launch the GUI
```

Requirements: Python ≥ 3.9, Windows/Linux/macOS for the GUI;
**IDEA StatiCa ≥ 24.1 (not Basic license)** for the CBFEM analysis
(the GUI also ships an offline *mock* of the REST service so every button can
be exercised without an IDEA installation).

### Using IDEA StatiCa

1. Start the REST service that ships with the desktop installation

   ```console
   cd "C:\Program Files\IDEA StatiCa\StatiCa 26.0"
   IdeaStatiCa.ConnectionRestApi.exe            # listens on http://localhost:5000
   ```

   (or let the SDK start it: *Settings → IDEA StatiCa connection…* and fill the
   *setup dir* – the app then uses `ConnectionApiServiceRunner`).
2. In the GUI: **Analysis → Run IDEA StatiCa analysis…** → *Run analysis*.
   The app then: builds the IOM XML → `POST /projects/import-iom-file` →
   adds the load effect (`POST …/load-effects`) → `POST …/connections/calculate`
   → reads `…/results` and `…/rawresults-text` → downloads the `.ideaCon`
   project and a PDF report into the output directory.

Without IDEA installed, tick **“Use built-in mock service”** in the same dialog:
a local stand-in implements the same REST endpoints (it evaluates the imported
IOM with the C&K ICR solver) so the complete client pipeline can be tested.

### Console tools

```bash
python -m eccbolt.validation        # prints the Crawford & Kulak verification table
python -m pytest tests -q           # unit + pipeline + GUI smoke tests
```

---

## 1b. Arbitrary (random) bolt coordinates - supported

The Connection API stores a bolt group as a **free list of hole positions**
(`BoltGrid.Positions : List<Point3D>` in the REST SDK, `<Positions>` in IOM) -
there is no regularity requirement, and optional per-position `SlottedHole`
entries exist as well.  The GUI therefore accepts any coordinates typed into
the bolt table **and** offers a *Random...* generator (rejection sampling with
a minimum spacing and a seed) in addition to the regular *Grid...* generator.
Random patterns are exported verbatim into the IOM and survive the whole
REST pipeline (covered by `tests/test_features.py`).

## 1c. Angled loads - resolved into force components

The REST API has **no load-angle parameter**: a load effect is defined by the
six member force components `N, Vy, Vz, Mx, My, Mz`
(`ConLoadEffectSectionLoad`).  The application therefore transforms the angled
load exactly as suggested: for an angle theta from vertical it applies

    Pv = P*cos(theta)   (vertical)      Ph = P*sin(theta)   (horizontal)
    N = -Pv,  Vz = Ph,  My = -P*(e*cos(theta) + h*sin(theta))

on the load-introduction member (and the exact opposite on the bearing
member), i.e. the horizontal + vertical components plus the resulting
in-plane moment at the bolt-group centroid.  The decomposition is displayed
in the results panel ("Load components Pv / Ph") and logged by the REST
workflow; `tests/test_features.py` checks the transformation against the
3-D cross product for theta = 0/30/45/90 deg, and proves that the (mock)
service recovers exactly the same bolt-group coefficient from those
components alone for an inclined load (Brandt Ex. 2).

---

## 2. How the connection is modelled for IDEA StatiCa

The public Connection API deliberately does not expose single plate/bolt edits;
connection geometry is authored through **IOM import** (`POST
…/projects/import-iom-file`).  That endpoint deserialises an
**`OpenModelContainer`** document (see the SDK signature
`Project.import_iom(container_xml_file=…)`: *“IdeaRS.OpenModel.OpenModelContainer
serialized to xml”*), so the GUI writes exactly that container – a bare
`<OpenModel>` root is rejected with HTTP 500.  Inside the container the model is
emitted in the **version-2 dialect** of `IdeaRS.OpenModel`
(`<Version>2</Version>`, plate material as a name string, bolt parameters inline
with `ConnectedPartIds`, `WeldData.ConnectedPartIds`, scalar
`EccentricityBeginX/Y/Z`), which is the syntax of the import fixtures shipped
with the Connection API SDK and the one the service's `IomVersioningService`
knows how to upgrade (versions 2.0.0 → 3.3.5).  Every element is written in the
member order of the .NET `XmlSerializer`, because out-of-order members are
silently dropped when the service deserialises the file.  The container holds:

| object | role |
|---|---|
| `Member1D` *SupportPlate* (rect. CSS) | supporting plate, continuous through the node, **bearing member** (supported) |
| `PlateData` *Loaded plate* | loaded plate with exact outline/thickness/position, face-to-face with the support plate |
| `BoltGrid` | user bolt coordinates, diameter, hole, grade (inline bolt parameters, as in the SDK fixtures); connects the two plates (single shear plane) |
| `Member1D` *LoadStub* + `WeldData` | short load-introduction stub welded to the loaded plate edge at the eccentricity (same device as IDEA’s own AISC “bracket plate” verification model) |
| `ConnectionPoint` | node at the **bolt-group centroid** |

Loads are internal forces **at the connection node** (IDEA applies them through
backward rigid links, see IDEA KB *“Principles of Loading in a Connection”*):
the stub receives `(V = P, M = P·(e·cosθ + h·sinθ))`, the bearing member the
exact opposite, so the load case is in equilibrium and the bolt group sees
precisely the shear + torsion of the physical system.

The IOM document is sent **UTF-8 encoded** (as in the official Connection-API
examples).  Should a service revision reject a variant with HTTP 500, the app
automatically retries the remaining compatibility variants
(`container-region2d` → `container-region` → `utf16-container-region2d` →
`openmodel-region2d`), logs every refusal with the service's problem+json
detail and stores each attempted document as `connection_iom_<variant>.xml` in
the output directory for inspection.  If every variant is refused, open the
`container-region2d` file in the IDEA StatiCa desktop application (it imports
the same container format) to see the exact parser message.

Generated artefacts per run: `connection_iom.xml`, the variant files,
`eccentric_connection.ideaCon`, `eccentric_connection_report.pdf`.

### Rules the service enforces on an imported IOM

These were established by importing candidate documents into a live IDEA
StatiCa Connection service (26.0, IOM 3.2.0) and reading back the converted
project (`export-iom`) and the service log
(`%LOCALAPPDATA%\Temp\IdeaStatica\Logs\ConnectionRestApi.log`):

* the uploaded XML must be an `OpenModelContainer`, otherwise HTTP 500;
* the `Version` string is upgraded by `IomVersioningService` - a document that
  claims a *newer* version than its content (or that contains `MatBoltGrade` /
  `BoltAssembly`, which upgrade step 2.0.5 creates itself) makes the upgrade
  throw and the import end in HTTP 500;
* a `ConnectedPartIds`/`ConnectedParts` entry that does not resolve to a part of
  the joint makes the service **silently drop that bolt grid** (and a dropped
  grid leaves the connected plate with "Not connected to the rest of the joint:
  … the analysis model is a mechanism");
* `PlateData` and `BoltGrid` must both be written with **local Z = global +X**
  (the service rejects other orientations: the plate is logged as
  *"… is not parallel"* and the grid is dropped);
* a `PlateData` must be perpendicular to the member it is welded/cut to, and
  every member of a joint must pass through or start at the connection node;
* `CutBeamByBeamData` entries must carry `WeldThickness` and `WeldType` when
  `IsWeld` is true (upgrade step 3.2.0 throws otherwise);
* a member whose cross-section is overstressed aborts the whole calculation
  (*"Connected member is overloaded"*) - no bolt result is produced at all, so
  the load-introduction member has to be stout.

The current generator satisfies the container/version/order rules above, so the
import succeeds; the CBFEM model itself (plate orientation, part references of
the bolt grid, stiff load introduction) is the remaining work - the service's
own message for that case is reported in the results panel.

### When the REST service is not running (connection refused)

Before talking to the SDK the app probes the service (TCP connect +
`GET /heartbeat`, the same endpoint the official SDK runner waits for).  If
nothing listens on the configured port:

* with **Settings > "Auto-start the REST service when it is not running"**
  (default ON) it searches `C:\Program Files\IDEA StatiCa\StatiCa *` for
  `IdeaStatiCa.ConnectionRestApi.exe`, launches it with `-port=<port>`,
  waits up to 120 s for the heartbeat and then continues; processes started
  this way are terminated when the application exits;
* otherwise a *friendly, actionable* message is shown instead of a raw
  traceback (install/license notes - the API needs a non-Basic license -,
  the exact manual start command, a hint about non-default ports such as
  `http://localhost:5193`, and the offline mock fallback);
* in the GUI the failure dialog offers **Auto-start & retry**, **Run with mock
  service instead** and **Open settings**; the settings dialog also has a
  **"Test connection now"** button that reports the heartbeat status and any
  IDEA installation found on the PC.

---

## 3. Crawford & Kulak (1971) solver (`eccbolt/icr.py`)

* load–deformation law `R = Rult·(1 − e^(−10·Δ))^0.55`, `Δmax = 0.34 in`;
* rigid plates, bolt deformations ∝ distance from the ICR, forces ⊥ radius;
* ICR found by damped Newton iteration on the two force-equilibrium equations
  (starting from the elastic centre, Brandt 1982); the moment equation gives
  `Pu = C·Rult`;
* elastic (vector) coefficient `Ce` reported for reference.

---

## 4. Verification (see `docs/verification_report.txt`)

| check | expected | obtained |
|---|---|---|
| Brandt (1982) Ex. 1 – 3 bolts, s=3″, e=4″, vertical | Cu = 1.40, Ce = 1.342 | 1.400 / 1.342 |
| Brandt (1982) Ex. 2 – 6 bolts, inclined load (0.6, −0.8) | Cu = 1.10, Ce = 0.942 | 1.095 / 0.943 |
| C-table generated from C&K (AISC 8th ed. / Guide Table 13.1), 308 entries | – | max dev. 2.3 % (table rounding) |
| scale invariance of C | exact | exact |
| concentric limit C = n | 6.000 | 6.000 |
| IOM export + REST pipeline (mock service) | 8 bolts | 8 bolts, max util. 0.98 |

Crawford & Kulak report that their predictions ran **5–14 % above** the eight
full-size tests (Guide §13.4) – the same bias is retained here because the
original coefficients (Rult = 74 kip double shear, Δu = 0.34 in) are used.

---

## 5. Windows executable

A pre-built single-file Windows executable (x64, no Python needed) is
distributed as five 18 MB parts plus a self-verifying reassembler (a single
94 MB file tends to get truncated by transfer channels, which makes Windows
report "This app can't run on your PC"):

    windows_exe/split_parts/EccentricBolt.exe.001 ... .005
    windows_exe/split_parts/reassemble_and_verify.bat
    windows_exe/EccentricBolt.exe.sha256      (SHA-256 of the complete exe)

Download all six files into one folder, double-click
`reassemble_and_verify.bat`; it produces `EccentricBolt.exe`
(94,254,432 bytes, SHA-256 fed9229a…152a34cdb) and refuses to continue if any
part is incomplete.

* double-click it to start the GUI (a console window shows the REST log);
* `EccentricBolt.exe --selftest` runs the headless verification suite
  (presets, C&K checks, IOM export, REST pipeline) and exits 0/1;
* `EccentricBolt.exe --smoke` builds the window headlessly and writes
  `smoke_gui.png` (used to validate the build).

The shipped binary passes both modes (`SELFTEST OK`, `SMOKE OK`).

### If Windows reports "This app can't run on your PC"

The published executable is a verified, uncorrupted x64 PE (SHA-256
`fed9229a…152a34cdb`, see `windows_exe/EccentricBolt.exe.sha256`).  That dialog
therefore almost always means one of:

1. **The copy on your PC is truncated/corrupted** (large single-file download).
   Check with `certutil -hashfile EccentricBolt.exe SHA256` (size must be
   94,254,432 bytes).  If it differs, use the transfer-robust delivery:
   download the five parts plus `reassemble_and_verify.bat` from
   `windows_exe/split_parts/` and double-click `reassemble_and_verify.bat` -
   it rebuilds the exe and verifies its SHA-256 before you run it.
2. **ARM64 device without x64 emulation** - Windows 11 IoT Enterprise LTSC
   2024 is also released for ARM64, and some IoT images ship without the
   x86-64 emulation package.  Check *Settings > System > About > System type*.
   If it reads ARM64, enable/repair emulation (DISM:
   `dism /online /enable-feature /featurename:Wow64 /all` is for x86; x64
   emulation is the `AMD64` emulation component - on LTSC ARM64 install the
   latest cumulative update or use the split-parts exe on an x64 machine).
   The application itself is x64-only.
3. **Smart App Control** (Windows Security > App & Browser control) blocking
   an unsigned executable - switch it off or add an exclusion.
4. File downloaded from the browser/e-mail: right-click > Properties >
   **Unblock**, then retry.

The shipped binary is produced on Windows with PyInstaller (single-file, Qt
limited to Core/Gui/Widgets + Agg backend) from `windows_exe/EccentricBolt.spec`
- double-click `windows_exe/build_windows_exe.bat` to rebuild it.  An earlier
release was additionally validated under Wine 11 (see
`docs/exe_gui_smoke_wine.png`).

---

## 6. Layout

```
main.py                  entry point
eccbolt/
  model.py               input data classes + C&K constants
  icr.py                 Crawford & Kulak ICR solver (+ elastic method)
  analysis.py            facade: C, Pu, utilization, bolt forces
  iom.py                 IOM OpenModel XML generator + load components
  idea_service.py        Connection REST API workflow (official SDK)
  mock_service.py        offline mock of the REST service
  validation.py          verification suite vs published C&K values
  presets.py             C&K/Brandt/AISC example inputs
  view3d.py / view2d.py  matplotlib 3-D / 2-D views
  gui.py                 PySide6 main window
tests/                   pytest suite (ICR, IOM, REST pipeline, GUI smoke)
docs/                    screenshots + verification report
```
