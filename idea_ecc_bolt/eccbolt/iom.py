"""
Generation of an IDEA StatiCa Open Model (IOM) document describing an
eccentrically loaded bolted steel shear connection, ready to be imported by the
IDEA StatiCa Connection REST API (``POST /api/N/projects/import-iom-file``).

Document format
---------------
The document is written in the **current** ``IdeaRS.OpenModel`` dialect that the
installed service itself exports (26.0 → ``<Version>3.2.0</Version>``).  The
element names and their order were taken from an IOM exported by the service
for a connection that contains bolt grids (``GET
.../connections/{id}/export-iom``), because ``XmlSerializer`` silently drops
every member that appears out of order:

* top level: ``Version, OriginSettings, Point3D, LineSegment3D, ArcSegment3D,
  PolyLine3D, Region3D, MatConcrete, MatReinforcement, MatSteel,
  MatPrestressSteel, MatWelding, MatBoltGrade, MatHeadedStudGrade,
  CrossSection, BoltAssembly, Pin, ... Element1D, Beam, Member1D, ...,
  ConnectionPoint, Connections``;
* ``Element1D`` carries the cross-section by reference
  (``CrossSectionBegin``/``CrossSectionEnd``) and vector eccentricities;
* ``BoltGrid`` is ``Id, Origin, AxisX/Y/Z, Positions, ConnectedParts
  (ReferenceElement: BeamData/PlateData + Id), Length, ShearInThread,
  BoltInteraction, BoltAssembly``;
* ``PlateData`` is ``Id, Name, Thickness, Material, Origin, AxisX/Y/Z,
  Geometry/Outline, OriginalModelId, IsNegativeObject``;
* ``WeldData`` is ``Id, Name, Thickness, Material, WeldMaterial, WeldType,
  IsIntermittent, ConnectedPartIds (original model ids as strings), Start, End``.

Model layout
------------
The connection is a **lap joint**: the supporting plate (bearing member) and the
loaded plate are both modelled as members whose mid-surfaces are face to face,
and one ``BoltGrid`` connects the two members' plates, i.e. exactly the
single-shear load path of the physical specimen.  The load is applied to the
loaded member through the Connection REST API (there is no load data in the
IOM): the loaded member receives ``N/Vz/My``, the bearing member the exact
opposite, so the bolt group sees the shear + torsion of the eccentric load.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import List, Tuple
from xml.sax.saxutils import escape

from .model import ConnectionInput

IOM_VERSION = "3.2.0"

# clearance between the two plate faces [m] - see IomBuilder.layout()
PLATE_GAP = 0.0


# --------------------------------------------------------------------------
# small XML helpers
# --------------------------------------------------------------------------
def _f(v: float) -> str:
    """Format a float the way .NET XmlSerializer does (round-trip)."""
    return repr(float(v))


def _vec(x, y, z, tag):
    return (f"<{tag}><X>{_f(x)}</X><Y>{_f(y)}</Y><Z>{_f(z)}</Z></{tag}>")


def _point(pid, x, y, z, name=None):
    nm = f"<Name>{name}</Name>" if name else "<Name />"
    return (f"<Point3D><Id>{pid}</Id>{nm}<X>{_f(x)}</X><Y>{_f(y)}</Y>"
            f"<Z>{_f(z)}</Z></Point3D>")


def _ref(type_name, oid):
    return f"<TypeName>{type_name}</TypeName><Id>{oid}</Id>"


def parse_open_model(xml_text: str) -> ET.Element:
    """Return the ``<OpenModel>`` element of an IOM document, accepting both
    an ``OpenModelContainer`` and a bare ``OpenModel`` root."""
    root = ET.fromstring(xml_text)
    if root.tag == "OpenModelContainer":
        inner = root.find("OpenModel")
        if inner is None:
            raise ValueError("OpenModelContainer without an OpenModel element")
        return inner
    return root


# --------------------------------------------------------------------------
# main generator
# --------------------------------------------------------------------------


class IomBuilder:
    """Builds the OpenModelContainer XML for a :class:`ConnectionInput`."""

    def __init__(self, ci: ConnectionInput):
        self.ci = ci
        self.points: List[Tuple[int, float, float, float, str]] = []
        self._pid = 0

    # -- geometry helpers -------------------------------------------------
    def add_point(self, x, y, z, name=None) -> int:
        self._pid += 1
        self.points.append((self._pid, x, y, z, name or f"N{self._pid}"))
        return self._pid

    # -- layout -----------------------------------------------------------
    def layout(self):
        """Returns a dict with all derived geometry (SI units).

        Both plates are members.  A small clearance is kept between the two
        plate faces: IDEA turns a zero-thickness interface into a "plate clash"
        warning (and then duplicates the bolts), while the bolts still transfer
        the load across the clearance.
        """
        ci = self.ci
        ta, tb = ci.plate_a.thickness, ci.plate_b.thickness
        gap = PLATE_GAP
        # the grid origin sits inside the first plate and the bolt spans the
        # grip of both plates
        return dict(ta=ta, tb=tb, gap=gap, y_bolt=0.0,
                    y_loaded=ta / 2 + gap + tb / 2,
                    bolt_len=ta / 2 + gap + tb + 0.005)

    # ----------------------------------------------------------------------
    def _materials_xml(self, country: str) -> Tuple[str, dict]:
        """MatSteel list (+ MatBoltGrade / MatWelding); returns (xml, ids)."""
        ci = self.ci
        steel_type = "MatSteelAISC" if country == "American" else "MatSteelEc2"
        mats, mat_ids = [], {}
        for plate in (ci.plate_a, ci.plate_b):
            key = (plate.material.designation, plate.material.fy, plate.material.fu)
            if key in mat_ids:
                continue
            mid = len(mats) + 1
            mat_ids[key] = mid
            m = plate.material
            mats.append(
                f"<MatSteel xsi:type=\"{steel_type}\"><Id>{mid}</Id>"
                f"<Name>{escape(m.designation)}</Name>"
                "<LoadFromLibrary>false</LoadFromLibrary>"
                f"<E>{_f(m.E)}</E><G>{_f(m.E / (2 * (1 + m.poisson)))}</G>"
                f"<Poisson>{_f(m.poisson)}</Poisson><UnitMass>7850</UnitMass>"
                "<SpecificHeat>490</SpecificHeat>"
                "<ThermalExpansion>1.2E-05</ThermalExpansion>"
                "<ThermalConductivity>45</ThermalConductivity>"
                "<IsDefaultMaterial>false</IsDefaultMaterial><OrderInCode>0</OrderInCode>"
                "<StateOfThermalExpansion>None</StateOfThermalExpansion>"
                "<StateOfThermalConductivity>None</StateOfThermalConductivity>"
                "<StateOfThermalSpecificHeat>None</StateOfThermalSpecificHeat>"
                "<StateOfThermalStressStrain>None</StateOfThermalStressStrain>"
                "<StateOfThermalStrain>None</StateOfThermalStrain>"
                "<UserThermalSpecificHeatCurvature><Points /></UserThermalSpecificHeatCurvature>"
                "<UserThermalConductivityCurvature><Points /></UserThermalConductivityCurvature>"
                "<UserThermalExpansionCurvature><Points /></UserThermalExpansionCurvature>"
                "<UserThermalStrainCurvature><Points /></UserThermalStrainCurvature>"
                "<UserThermalStressStrainCurvature />"
                f"<fy>{_f(m.fy)}</fy><fu>{_f(m.fu)}</fu>"
                f"<fy40>{_f(m.fy)}</fy40><fu40>{_f(m.fu)}</fu40></MatSteel>")

        grade = "MaterialBoltGradeAISC" if country == "American" else "MaterialBoltGradeEc2"
        bm = ci.bolt_mat
        bolt_grade = (
            f"<MaterialBoltGrade xsi:type=\"{grade}\"><Id>1</Id>"
            f"<Name>{escape(bm.designation)}</Name>"
            "<LoadFromLibrary>false</LoadFromLibrary>"
            f"<E>{_f(210e9)}</E><G>{_f(210e9 / 2.6)}</G>"
            "<Poisson>0.3</Poisson><UnitMass>0</UnitMass>"
            "<SpecificHeat>490</SpecificHeat><ThermalExpansion>1.4E-04</ThermalExpansion>"
            "<ThermalConductivity>45</ThermalConductivity>"
            "<IsDefaultMaterial>false</IsDefaultMaterial><OrderInCode>0</OrderInCode>"
            "<StateOfThermalExpansion>None</StateOfThermalExpansion>"
            "<StateOfThermalConductivity>None</StateOfThermalConductivity>"
            "<StateOfThermalSpecificHeat>None</StateOfThermalSpecificHeat>"
            "<StateOfThermalStressStrain>None</StateOfThermalStressStrain>"
            "<StateOfThermalStrain>None</StateOfThermalStrain>"
            f"<fub>{_f(bm.fub)}</fub><fyb>{_f(bm.fyb)}</fyb>"
            f"<Elongation>0.18</Elongation><Code>{country}</Code>"
            "</MaterialBoltGrade>")

        weld_mat = ("<MatWelding><Id>1</Id><Name>E70xx</Name>"
                    "<LoadFromLibrary>false</LoadFromLibrary>"
                    f"<E>{_f(210e9)}</E><G>{_f(210e9 / 2.6)}</G>"
                    "<Poisson>0.3</Poisson><UnitMass>7850</UnitMass>"
                    "<SpecificHeat>490</SpecificHeat>"
                    "<ThermalExpansion>1.2E-05</ThermalExpansion>"
                    "<ThermalConductivity>45</ThermalConductivity>"
                    "<IsDefaultMaterial>false</IsDefaultMaterial><OrderInCode>0</OrderInCode>"
                    "<StateOfThermalExpansion>None</StateOfThermalExpansion>"
                    "<StateOfThermalConductivity>None</StateOfThermalConductivity>"
                    "<StateOfThermalSpecificHeat>None</StateOfThermalSpecificHeat>"
                    "<StateOfThermalStressStrain>None</StateOfThermalStressStrain>"
                    "<StateOfThermalStrain>None</StateOfThermalStrain>"
                    f"<Fexx>{_f(483e6)}</Fexx></MatWelding>")
        return "".join(mats), mat_ids, bolt_grade, weld_mat

    # ----------------------------------------------------------------------
    def _bolt_assembly_xml(self) -> Tuple[str, float]:
        """The ``<BoltAssembly>`` list of the OpenModel (container + item).

        The top-level ``<BoltAssembly>`` element is the *list* property of
        ``OpenModel`` - the assembly itself is nested inside it, exactly like
        ``<MatSteel><MatSteel …/></MatSteel>``.  Writing the item without its
        wrapper leaves the bolt grid's ``BoltAssembly`` reference dangling and
        the service then throws while importing the grid.
        """
        ci = self.ci
        d = ci.bolt_diameter
        hole = d + ci.bolt_hole_clearance
        ab = math.pi / 4 * d ** 2
        at = 0.78 * ab
        xml = (
            "<BoltAssembly><BoltAssembly><Id>1</Id>"
            f"<Name>{escape(ci.bolt_mat.designation)} {d*1000:.0f}</Name>"
            f"<BoltGrade>{_ref('MaterialBoltGrade', 1)}</BoltGrade>"
            f"<Diameter>{_f(d)}</Diameter><Borehole>{_f(hole)}</Borehole>"
            f"<HeadDiameter>{_f(1.6 * d)}</HeadDiameter>"
            f"<DiagonalHeadDiameter>{_f(1.7 * d)}</DiagonalHeadDiameter>"
            f"<HeadHeight>{_f(0.65 * d)}</HeadHeight>"
            f"<GrossArea>{_f(ab)}</GrossArea>"
            f"<TensileStressArea>{_f(at)}</TensileStressArea>"
            f"<NutThickness>{_f(0.8 * d)}</NutThickness>"
            f"<WasherThickness>{_f(0.005)}</WasherThickness>"
            "<WasherAtHead>true</WasherAtHead><WasherAtNut>true</WasherAtNut>"
            "<LoadFromLibrary>false</LoadFromLibrary></BoltAssembly></BoltAssembly>")
        return xml, ab

    # ----------------------------------------------------------------------
    def _open_model(self) -> str:
        ci = self.ci
        L = self.layout()
        country = "American" if ci.design_code.upper().startswith("AISC") else "ECEN"

        mats_xml, mat_ids, bolt_grade, weld_mat = self._materials_xml(country)
        bolt_assembly, _ = self._bolt_assembly_xml()
        mat_a = mat_ids[(ci.plate_a.material.designation, ci.plate_a.material.fy,
                         ci.plate_a.material.fu)]
        mat_b = mat_ids[(ci.plate_b.material.designation, ci.plate_b.material.fy,
                         ci.plate_b.material.fu)]

        # ---- rectangular cross-sections (plate members) -------------------
        #  With the member local system used below (x = +Z, y = -Y, z = +X)
        #  the member plate spans local x-y, i.e. global Z-Y, and the CSS
        #  "Height" becomes the plate thickness in X.  Height = w, Width = t
        #  makes the member a plate-shaped body that contains the whole bolt
        #  group (the service requires the bolt centres to lie inside every
        #  plate they intersect - see "Bolts are too close to plate edge").
        def css(cid, w, t, mref, nm):
            return (
                f"<CrossSection xsi:type=\"CrossSectionParameter\"><Id>{cid}</Id>"
                f"<Name>{nm} {w*1000:.0f}x{t*1000:.0f}</Name>"
                "<CrossSectionRotation>0</CrossSectionRotation>"
                "<IsInPrincipal>false</IsInPrincipal>"
                "<CrossSectionType>Rect</CrossSectionType><Parameters>"
                f"<Parameter xsi:type=\"ParameterDouble\"><Name>Width</Name>"
                f"<Value>{_f(t)}</Value></Parameter>"
                f"<Parameter xsi:type=\"ParameterDouble\"><Name>Height</Name>"
                f"<Value>{_f(w)}</Value></Parameter></Parameters>"
                f"<Material>{_ref('MatSteel', mref)}</Material></CrossSection>")

        css_xml = css(1, ci.plate_a.width, L['ta'], mat_a, "PL-A") + \
                  css(2, ci.plate_b.width, L['tb'], mat_b, "PL-B")

        # ---- nodes / segments / elements / members -----------------------
        #  member 1: supporting plate, centred on the connection node
        #  member 2: loaded plate, shifted in Y so its face touches member 1
        p_a_bot = self.add_point(0, 0, -ci.plate_a.height / 2, "A_bot")
        p_node = self.add_point(0, 0, 0, "NODE")
        p_a_top = self.add_point(0, 0, ci.plate_a.height / 2, "A_top")
        p_b_bot = self.add_point(0, L['y_loaded'],
                                 -ci.plate_b.height / 2, "B_bot")
        p_b_top = self.add_point(0, L['y_loaded'],
                                 ci.plate_b.height / 2, "B_top")

        # local system: x along the member (+Z), y = -Y, z = +X (right handed).
        #  This is the only member orientation for which this service build
        #  keeps the bolt grid: with any other axes `import-iom-file` silently
        #  drops the grid (no bolts, no holes, no operation in the tree).
        lcs = ("<LocalCoordinateSystem xsi:type=\"CoordSystemByVector\">"
               f"{_vec(0, 0, 1, 'VecX')}{_vec(0, -1, 0, 'VecY')}"
               f"{_vec(1, 0, 0, 'VecZ')}</LocalCoordinateSystem>")

        def segment(sid, p1, p2):
            return (f"<LineSegment3D><Id>{sid}</Id>"
                    f"<StartPoint>{_ref('Point3D', p1)}</StartPoint>"
                    f"<EndPoint>{_ref('Point3D', p2)}</EndPoint>{lcs}"
                    "</LineSegment3D>")

        seg_xml = [segment(1, p_a_bot, p_node), segment(2, p_node, p_a_top),
                   segment(3, p_b_bot, p_node), segment(4, p_node, p_b_top)]

        def element(eid, sid, css_id):
            return (f"<Element1D><Id>{eid}</Id><Name>E{eid}</Name>"
                    f"<CrossSectionBegin>{_ref('CrossSection', css_id)}</CrossSectionBegin>"
                    f"<CrossSectionEnd>{_ref('CrossSection', css_id)}</CrossSectionEnd>"
                    f"<Segment>{_ref('LineSegment3D', sid)}</Segment>"
                    "<RotationRx>0</RotationRx>"
                    f"{_vec(0, 0, 0, 'EccentricityBegin')}"
                    f"{_vec(0, 0, 0, 'EccentricityEnd')}</Element1D>")

        el_xml = [element(1, 1, 1), element(2, 2, 1),
                  element(3, 3, 2), element(4, 4, 2)]

        def member(mid, name, elem_ids, mtype):
            refs = "".join(f"<ReferenceElement>{_ref('Element1D', e)}</ReferenceElement>"
                           for e in elem_ids)
            return (f"<Member1D><Id>{mid}</Id><Name>{name}</Name>"
                    f"<Elements1D>{refs}</Elements1D>"
                    f"<Member1DType>{mtype}</Member1DType>"
                    "<Alignment>Center</Alignment><MirrorY>false</MirrorY>"
                    "<MirrorZ>false</MirrorZ>"
                    f"{_vec(0, 0, 0, 'EccentricityBegin')}"
                    f"{_vec(0, 0, 0, 'EccentricityEnd')}"
                    "<InsertionPoint>CenterOfGravity</InsertionPoint>"
                    "<EccentricityReference>LocalCoordinateSystem</EccentricityReference>"
                    "</Member1D>")

        mem_xml = [member(1, "SupportPlate", [1, 2], "Column"),
                   member(2, "LoadedPlate", [3, 4], "Beam")]

        # ---- connection point -------------------------------------------
        conn_point = (
            f"<ConnectionPoint><Id>1</Id><Name>CON1</Name>"
            f"<Node>{_ref('Point3D', p_node)}</Node><ConnectedMembers>"
            f"<ConnectedMember><Id>1</Id><MemberId>{_ref('Member1D', 1)}</MemberId>"
            "<IsContinuous>true</IsContinuous><Length>0</Length></ConnectedMember>"
            f"<ConnectedMember><Id>2</Id><MemberId>{_ref('Member1D', 2)}</MemberId>"
            "<IsContinuous>false</IsContinuous><Length>0</Length></ConnectedMember>"
            "</ConnectedMembers><NodeId>0</NodeId></ConnectionPoint>")

        # ---- beams -------------------------------------------------------
        def beam_data(bid, name, bearing):
            return (f"<BeamData><Id>{bid}</Id><Name>{name}</Name>"
                    f"<OriginalModelId>Beam{bid}</OriginalModelId>"
                    "<IsAdded>false</IsAdded><AddedMemberLength>0</AddedMemberLength>"
                    "<IsNegativeObject>false</IsNegativeObject><MirrorY>false</MirrorY>"
                    "<MirrorZ>false</MirrorZ><RefLineInCenterOfGravity>false"
                    "</RefLineInCenterOfGravity>"
                    f"<IsBearingMember>{'true' if bearing else 'false'}</IsBearingMember>"
                    "<AutoAddCutByWorkplane>false</AutoAddCutByWorkplane></BeamData>")

        beams_xml = ("<Beams>" + beam_data(1, "SupportPlate", True)
                     + beam_data(2, "LoadedPlate", False) + "</Beams>")

        # ---- bolt grid ----------------------------------------------------
        d = ci.bolt_diameter
        yb = L['y_bolt']
        positions = "".join(
            f"<Point3D><Id>{i+1}</Id><X>{_f(bx)}</X><Y>{_f(yb)}</Y>"
            f"<Z>{_f(by)}</Z></Point3D>" for i, (bx, by) in enumerate(ci.bolts_xy))
        parts = (f"<ReferenceElement>{_ref('BeamData', 1)}</ReferenceElement>"
                 f"<ReferenceElement>{_ref('BeamData', 2)}</ReferenceElement>")
        bolts_xml = (
            "<BoltGrids><BoltGrid><Id>1</Id>"
            f"<Origin><Id>0</Id><X>0</X><Y>{_f(yb)}</Y><Z>0</Z></Origin>"
            f"{_vec(0, 1, 0, 'AxisX')}{_vec(0, 0, 1, 'AxisY')}{_vec(1, 0, 0, 'AxisZ')}"
            f"<Positions>{positions}</Positions>"
            f"<ConnectedParts>{parts}</ConnectedParts>"
            f"<Length>{_f(L['bolt_len'])}</Length>"
            f"<ShearInThread>{'true' if ci.threads_in_shear_plane else 'false'}</ShearInThread>"
            "<BoltInteraction>Interaction</BoltInteraction>"
            f"<BoltAssembly>{_ref('BoltAssembly', 1)}</BoltAssembly>"
            "</BoltGrid></BoltGrids>")

        connection_xml = (
            "<Connections><ConnectionData>"
            f"<ConnectionPoint>{_ref('ConnectionPoint', 1)}</ConnectionPoint>"
            f"{beams_xml}<Plates /><FoldedPlates />{bolts_xml}<AnchorGrids />"
            "<Welds /><ConcreteBlocks /><CutBeamByBeams />"
            "</ConnectionData></Connections>")

        # ---- assemble -----------------------------------------------------
        points_xml = "".join(_point(pid, x, y, z, nm)
                             for pid, x, y, z, nm in self.points)
        return (
            "<OpenModel>"
            f"<Version>{IOM_VERSION}</Version>"
            "<OriginSettings>"
            "<ProjectName>EccentricBoltedShearConnection</ProjectName>"
            f"<ProjectDescription>{escape(ci.title)}</ProjectDescription>"
            "<DateOfCreate>0001-01-01T00:00:00</DateOfCreate>"
            "<CrossSectionConversionTable>NoUsed</CrossSectionConversionTable>"
            f"<CountryCode>{country}</CountryCode>"
            "<ImportRecommendedWelds>false</ImportRecommendedWelds>"
            "<CheckEquilibrium>true</CheckEquilibrium>"
            "</OriginSettings>"
            f"<Point3D>{points_xml}</Point3D>"
            f"<LineSegment3D>{''.join(seg_xml)}</LineSegment3D>"
            "<ArcSegment3D /><PolyLine3D /><Region3D />"
            "<MatConcrete /><MatReinforcement />"
            f"<MatSteel>{mats_xml}</MatSteel>"
            f"<MatPrestressSteel />{weld_mat}"
            f"<MatBoltGrade>{bolt_grade}</MatBoltGrade>"
            "<MatHeadedStudGrade />"
            f"<CrossSection>{css_xml}</CrossSection>"
            f"{bolt_assembly}<Pin />"
            "<ReinforcedCrossSection /><HingeElement1D /><Opening /><DappedEnd />"
            "<PatchDevice />"
            f"<Element1D>{''.join(el_xml)}</Element1D>"
            "<Beam />"
            f"<Member1D>{''.join(mem_xml)}</Member1D>"
            "<Element2D /><Wall /><Member2D /><RigidLink /><PointOnLine3D />"
            "<PointSupportNode /><LineSupportSegment /><LoadsInPoint /><LoadsOnLine />"
            "<StrainLoadsOnLine /><PointLoadsOnLine /><LoadsOnSurface /><Settlements />"
            "<TemperatureLoadsOnLine /><LoadGroup /><LoadCase /><CombiInput />"
            "<Attribute />"
            f"<ConnectionPoint>{conn_point}</ConnectionPoint>"
            f"{connection_xml}"
            "<Reinforcement /><ISDModel /><InitialImperfectionOfPoint /><Tendon />"
            "<ResultClass /><DesignMember /><SubStructure /><CheckMember />"
            "<ConcreteCheckSection />"
            "<RebarShape /><RebarGeneral /><RebarSingle /><RebarStirrups />"
            "<Taper /><Span /><SolidBlocks3D /><SurfaceSupports3D /><BasePlates3D />"
            "<Anchors3D /><DetailLoadCase /><DetailCombination />"
            "</OpenModel>")

    # ----------------------------------------------------------------------
    def build(self) -> str:
        """The OpenModelContainer document (UTF-8 declaration)."""
        return ('<?xml version="1.0" encoding="utf-8"?>\r\n'
                '<OpenModelContainer xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\r\n'
                + self._open_model() + "\r\n"
                "<OpenModelResult />\r\n"
                "</OpenModelContainer>\r\n")

    # ----------------------------------------------------------------------
    def variants(self):
        """(label, xml_text) pairs tried in order by the REST workflow."""
        return [("container-3.2.0", self.build()),
                ("container-3.2.0-utf16",
                 self.build().replace('encoding="utf-8"', 'encoding="utf-16"'))]

    def build_bytes(self, variant: str = "container-3.2.0") -> bytes:
        xml = dict(self.variants())[variant]
        enc = "utf-16" if variant.endswith("utf16") else "utf-8"
        return xml.encode(enc)

    def write(self, path: str, variant: str = "container-3.2.0") -> str:
        with open(path, "wb") as fh:
            fh.write(self.build_bytes(variant))
        return path


def member_load_components(ci: ConnectionInput):
    """Internal force components (N, Vy, Vz, Mx, My, Mz) in the local systems of
    member 1 (support plate) and member 2 (loaded plate) that represent the
    eccentric load at the connection node.

    Both members use the IOM ``CoordSystemByVector`` local system of
    :mod:`eccbolt.iom` (x_local = +Z, y_local = -Y, z_local = +X), so the
    member forces of a load whose global components are ``F = (px, 0, pz)``
    with the global moment ``M = (0, my_g, 0)`` are

        n  =  Fz        vy =  0        vz = Fx
        mx =  0         my = -my_g     mz = 0

    The load acts at (e, h) in the connection plane and its line of action has
    the perpendicular lever arm ``e*cos(theta) + h*sin(theta)`` from the
    bolt-group centroid.
    """
    P = ci.load.P
    t = math.radians(ci.load.theta)
    px, pz = P * math.sin(t), -P * math.cos(t)      # global X, Z
    h = ci.load.h_load if ci.load.h_load is not None else (
        ci.plate_b.cy + ci.plate_b.height / 2)
    my_g = P * (ci.load.e * math.cos(t) + h * math.sin(t))   # about +Y global
    # member 2 carries the load, member 1 the reaction
    loaded = dict(n=pz, vy=0.0, vz=px, mx=0.0, my=-my_g, mz=0.0)
    support = dict(n=-pz, vy=0.0, vz=-px, mx=0.0, my=my_g, mz=0.0)
    return support, loaded
