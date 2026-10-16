"""
Generation of an IDEA StatiCa Open Model (IOM) document describing the
eccentrically loaded bolted shear connection, ready to be imported by the
IDEA StatiCa Connection REST API (``POST /api/N/projects/import-iom-file``).

Document format
---------------
The import endpoint expects an **``OpenModelContainer``** document (the
container is what ``IdeaRS.OpenModel.Tools.OpenModelContainerToXml`` writes and
what every official Connection-API example / SDK integration test uploads);
a bare ``<OpenModel>`` root is rejected by the service.

Inside the container the model is written in the *version 2* dialect of
``IdeaRS.OpenModel`` - the syntax used by the official import fixtures shipped
with the Connection API SDK (``IOM/OpenModelContainer.xml``,
``ST_ConRestApiClient/Projects/OneConnectionImport.xml``):

* ``<Version>2</Version>`` - the service's ``IomVersioningService`` upgrades the
  document from 2.0.0 to the version of the installed application (this is the
  supported, tested upgrade path; declaring a *newer* version than the service
  knows - or a hybrid document that claims to be current - makes the upgrade
  steps fail, which surfaces as HTTP 500 "An unexpected error occurred");
* ``PlateData/Material`` is the **material name** (a string), not a reference;
* ``BoltGrid`` carries the bolt parameters inline (diameter, head, bore hole,
  ...), the bolt grade as a plain ``<Material>`` name and ``ConnectedPartIds`` -
  the ``OriginalModelId`` values of the connected objects.  ``MatBoltGrade`` and
  ``BoltAssembly`` must **not** be written: upgrade step 2.0.5 builds both from
  these grid parameters and fails with "an item with the same key has already
  been added" if the lists are already there;
* ``WeldData`` uses ``ConnectedPartIds`` as well;
* ``Element1D`` eccentricities are the scalar ``EccentricityBeginX/Y/Z`` /
  ``EccentricityEndX/Y/Z`` triplets (converted to vectors by upgrade step 3.0);
* every element is emitted in the exact order in which ``XmlSerializer``
  writes the corresponding ``IdeaRS.OpenModel`` class - out-of-order members
  are silently dropped by the .NET deserializer.

Model layout (unchanged): the supporting plate is a member with a solid
rectangular cross-section (bearing member), the loaded plate is a
``PlateData`` component, one ``BoltGrid`` connects them and a short
load-introduction stub member is welded to the loaded plate edge.
"""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from typing import List, Tuple
from xml.sax.saxutils import escape

from .model import ConnectionInput

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
        """Returns a dict with all derived geometry (SI units)."""
        ci = self.ci
        ta, tb = ci.plate_a.thickness, ci.plate_b.thickness
        ya = 0.0                                  # plate A mid-surface
        yb = ta / 2 + tb / 2                      # plate B mid-surface
        yint = ta / 2                             # A/B interface (bolt plane)
        tc = max(0.008, tb)                       # load stub thickness
        yc = yb + tb / 2 + tc / 2                 # stub mid-surface
        e = ci.load.e
        h = ci.load.h_load
        if h is None:
            h = ci.plate_b.cy + ci.plate_b.height / 2   # top edge of plate B
        return dict(ta=ta, tb=tb, tc=tc, ya=ya, yb=yb, yint=yint, yc=yc,
                    e=e, h=h)

    # ----------------------------------------------------------------------
    def _materials_xml(self, country: str) -> Tuple[str, dict]:
        """MatSteel list; returns (xml, material ids).

        The bolt grade / bolt assembly are deliberately *not* written: upgrade
        step 2.0.5 of the service creates ``MatBoltGrade`` and ``BoltAssembly``
        from the parameters in the bolt grid (and fails if the document already
        contains them), which is why the version-2 fixtures carry the grade as a
        plain ``<Material>`` name inside the grid only.
        """
        ci = self.ci
        mat_steel_cls = "MatSteelAISC" if country == "American" else "MatSteelEc2"
        mats = []
        mat_ids = {}
        for plate in (ci.plate_a, ci.plate_b):
            key = (plate.material.designation, plate.material.fy, plate.material.fu)
            if key in mat_ids:
                continue
            mid = len(mats) + 1
            mat_ids[key] = mid
            m = plate.material
            diagram = "<DiagramType>Bilinear</DiagramType>" if mat_steel_cls == "MatSteelEc2" else ""
            mats.append(
                f"<MatSteel xsi:type=\"{mat_steel_cls}\"><Id>{mid}</Id>"
                f"<Name>{escape(m.designation)}</Name>"
                "<LoadFromLibrary>false</LoadFromLibrary>"
                f"<E>{_f(m.E)}</E><G>{_f(m.E / (2 * (1 + m.poisson)))}</G>"
                f"<Poisson>{_f(m.poisson)}</Poisson><UnitMass>7850</UnitMass>"
                "<SpecificHeat>0.6</SpecificHeat>"
                "<ThermalExpansion>1.2E-05</ThermalExpansion>"
                "<ThermalConductivity>45</ThermalConductivity>"
                "<IsDefaultMaterial>false</IsDefaultMaterial><OrderInCode>0</OrderInCode>"
                "<StateOfThermalExpansion>Code</StateOfThermalExpansion>"
                "<StateOfThermalConductivity>Code</StateOfThermalConductivity>"
                "<StateOfThermalSpecificHeat>Code</StateOfThermalSpecificHeat>"
                "<StateOfThermalStressStrain>Code</StateOfThermalStressStrain>"
                "<StateOfThermalStrain>Code</StateOfThermalStrain>"
                f"<fy>{_f(m.fy)}</fy><fu>{_f(m.fu)}</fu>"
                f"<fy40>{_f(m.fy)}</fy40><fu40>{_f(m.fu)}</fu40>{diagram}</MatSteel>")
        return "".join(mats), mat_ids

    # ----------------------------------------------------------------------
    def _open_model(self, plate_geometry: str = "region2d") -> str:
        """The ``<OpenModel>`` element (no XML declaration), version-2 dialect.

        plate_geometry: 'region2d' (explicit Region2D outline - what the
        official fixtures use) or 'region' (SVG path string).
        """
        ci = self.ci
        L = self.layout()
        code = ci.design_code.upper()
        country = "American" if code.startswith("AISC") else "ECEN"

        mats_xml, mat_ids = self._materials_xml(country)
        mat_a = mat_ids[(ci.plate_a.material.designation, ci.plate_a.material.fy,
                         ci.plate_a.material.fu)]
        mat_b = mat_ids[(ci.plate_b.material.designation, ci.plate_b.material.fy,
                         ci.plate_b.material.fu)]

        # ---- cross sections (rectangular plate members) ------------------
        css_xml = []
        for cid, (w, t, mref, nm) in enumerate([
                (ci.plate_a.width, L['ta'], mat_a, "PL-A"),
                (max(0.10, ci.plate_b.width * 0.5), L['tc'], mat_b, "PL-C")], start=1):
            css_xml.append(
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

        # ---- nodes / segments / elements / members -----------------------
        za_top = ci.plate_a.cy + ci.plate_a.height / 2
        za_bot = ci.plate_a.cy - ci.plate_a.height / 2
        p_a_bot = self.add_point(0, 0, za_bot, "A_bot")
        p_node = self.add_point(0, 0, 0, "NODE")
        p_a_top = self.add_point(0, 0, za_top, "A_top")
        zc_top = L['h'] + 0.05
        p_c_end = self.add_point(L['e'], L['yc'], zc_top, "C_end")

        seg_xml, el_xml, mem_xml = [], [], []

        def segment(sid, p1, p2, lcs=True):
            lcsx = ""
            if lcs:
                lcsx = ("<LocalCoordinateSystem xsi:type=\"CoordSystemByPoint\">"
                        "<Point><Id>0</Id><X>100000</X><Y>0</Y><Z>0</Z></Point>"
                        "<InPlane>ZX</InPlane></LocalCoordinateSystem>")
            seg_xml.append(
                f"<LineSegment3D><Id>{sid}</Id><StartPoint>{_ref('Point3D', p1)}"
                f"</StartPoint><EndPoint>{_ref('Point3D', p2)}</EndPoint>{lcsx}"
                "</LineSegment3D>")

        def element(eid, sid, css_id):
            # Element1D - member order as XmlSerializer writes it (version-2
            # scalar eccentricities, converted by upgrade step 3.0)
            el_xml.append(
                f"<Element1D><Id>{eid}</Id><Name>E{eid}</Name>"
                f"<Segment>{_ref('LineSegment3D', sid)}</Segment><RotationRx>0</RotationRx>"
                "<EccentricityBeginX>0</EccentricityBeginX><EccentricityBeginY>0</EccentricityBeginY>"
                "<EccentricityBeginZ>0</EccentricityBeginZ><EccentricityEndX>0</EccentricityEndX>"
                "<EccentricityEndY>0</EccentricityEndY><EccentricityEndZ>0</EccentricityEndZ>"
                "</Element1D>")

        def member(mid, name, elem_ids, css_id, mtype):
            refs = "".join(f"<ReferenceElement>{_ref('Element1D', e)}</ReferenceElement>"
                           for e in elem_ids)
            mem_xml.append(
                f"<Member1D><Id>{mid}</Id><Name>{name}</Name>"
                f"<Elements1D>{refs}</Elements1D>"
                f"<Member1DType>{mtype}</Member1DType>"
                f"<CrossSection>{_ref('CrossSection', css_id)}</CrossSection>"
                "<Alignment>Center</Alignment><MirrorY>false</MirrorY>"
                "<MirrorZ>false</MirrorZ></Member1D>")

        # member A (supporting plate): bottom->node->top, member C (load stub)
        segment(1, p_a_bot, p_node)
        segment(2, p_node, p_a_top)
        segment(3, p_node, p_c_end)
        element(1, 1, 1)
        element(2, 2, 1)
        element(3, 3, 2)
        member(1, "SupportPlate", [1, 2], 1, "Column")
        member(2, "LoadStub", [3], 2, "Beam")

        # ---- connection point -------------------------------------------
        conn_point = (
            f"<ConnectionPoint><Id>1</Id><Name>CON1</Name>"
            f"<Node>{_ref('Point3D', p_node)}</Node><ConnectedMembers>"
            f"<ConnectedMember><Id>1</Id><MemberId>{_ref('Member1D', 1)}</MemberId>"
            "<IsContinuous>true</IsContinuous><Length>0</Length></ConnectedMember>"
            f"<ConnectedMember><Id>2</Id><MemberId>{_ref('Member1D', 2)}</MemberId>"
            "<IsContinuous>false</IsContinuous><Length>0</Length></ConnectedMember>"
            "</ConnectedMembers><NodeId>0</NodeId></ConnectionPoint>")

        # ---- beams (short member descriptions of the connection) ---------
        def beam_data(bid, name, bearing):
            return (f"<BeamData><Id>{bid}</Id><Name>{name}</Name><Plates />"
                    f"<OriginalModelId>{bid}</OriginalModelId><Cuts />"
                    "<IsAdded>false</IsAdded><AddedMemberLength>0</AddedMemberLength>"
                    "<IsNegativeObject>false</IsNegativeObject><MirrorY>false</MirrorY>"
                    "<RefLineInCenterOfGravity>true</RefLineInCenterOfGravity>"
                    f"<IsBearingMember>{'true' if bearing else 'false'}</IsBearingMember>"
                    "<AutoAddCutByWorkplane>false</AutoAddCutByWorkplane></BeamData>")

        beams_xml = ("<Beams>" + beam_data(1, "SupportPlate", True)
                     + beam_data(2, "LoadStub", False) + "</Beams>")

        # ---- plate B (loaded plate) --------------------------------------
        ob = ci.plate_b.outline()
        x0, z0 = ob[0]
        w, h = ci.plate_b.width, ci.plate_b.height
        if plate_geometry == "region2d":
            seg = ('<Segment2D xsi:type="LineSegment2D"><EndPoint>'
                   '<X>{:.17g}</X><Y>{:.17g}</Y></EndPoint></Segment2D>')
            geo = ("<Geometry><Outline><StartPoint><X>0</X><Y>0</Y></StartPoint>"
                   "<Segments>"
                   + seg.format(w, 0) + seg.format(w, h) + seg.format(0, h)
                   + seg.format(0, 0)
                   + "</Segments></Outline></Geometry>")
        else:
            geo = f"<Region>M 0 0 L {_f(w)} 0 L {_f(w)} {_f(h)} L 0 {_f(h)} L 0 0</Region>"
        plates_xml = (
            "<Plates><PlateData><Id>11</Id><Name>"
            f"{escape(ci.plate_b.name)}</Name><Thickness>{_f(L['tb'])}</Thickness>"
            f"<Material>{escape(ci.plate_b.material.designation)}</Material>"
            "<OutlinePoints />"
            f"<Origin><Id>0</Id><X>{_f(x0)}</X><Y>{_f(L['yb'])}</Y><Z>{_f(z0)}</Z></Origin>"
            f"{_vec(1, 0, 0, 'AxisX')}{_vec(0, 0, 1, 'AxisY')}{_vec(0, -1, 0, 'AxisZ')}"
            f"{geo}<OriginalModelId>11</OriginalModelId>"
            "<IsNegativeObject>false</IsNegativeObject></PlateData></Plates>")

        # ---- bolt grid (bolt parameters inline + connected part ids) ------
        d = ci.bolt_diameter
        hole = d + ci.bolt_hole_clearance
        ab_mm2 = math.pi / 4 * d ** 2 * 1e6        # gross area [mm2]
        at_mm2 = 0.78 * ab_mm2                     # tensile stress area [mm2]
        pos = "".join(
            f"<Point3D><Id>0</Id><Name /><X>{_f(bx)}</X><Y>{_f(L['yint'])}</Y>"
            f"<Z>{_f(by)}</Z></Point3D>"
            for bx, by in ci.bolts_xy)
        bolts_xml = (
            "<BoltGrids><BoltGrid><Id>41</Id>"
            "<IsAnchor>false</IsAnchor><AnchorLen>0</AnchorLen>"
            f"<HoleDiameter>{_f(hole)}</HoleDiameter><Diameter>{_f(d)}</Diameter>"
            f"<HeadDiameter>{_f(1.6*d)}</HeadDiameter>"
            f"<DiagonalHeadDiameter>{_f(1.7*d)}</DiagonalHeadDiameter>"
            f"<HeadHeight>{_f(0.65*d)}</HeadHeight><BoreHole>{_f(hole)}</BoreHole>"
            f"<TensileStressArea>{_f(at_mm2)}</TensileStressArea>"
            f"<NutThickness>{_f(0.8*d)}</NutThickness>"
            "<BoltAssemblyName /><Standard />"
            f"<Material>{escape(ci.bolt_mat.designation)}</Material>"
            f"<Origin><Id>0</Id><Name /><X>0</X><Y>{_f(L['yint'])}</Y><Z>0</Z></Origin>"
            f"{_vec(0, 1, 0, 'AxisX')}{_vec(0, 0, 1, 'AxisY')}{_vec(1, 0, 0, 'AxisZ')}"
            f"<Positions>{pos}</Positions><ConnectedPlates />"
            "<ConnectedPartIds><string>1</string><string>11</string></ConnectedPartIds>"
            f"<ShearInThread>{'true' if ci.threads_in_shear_plane else 'false'}</ShearInThread>"
            "<BoltInteraction>Interaction</BoltInteraction></BoltGrid></BoltGrids>")

        # ---- weld: load stub C to plate B (fillet weld along the contact) --
        zw0 = max(0.0, L['h'] - 0.15)
        welds_xml = (
            "<Welds><WeldData><Id>31</Id>"
            f"<Thickness>{_f(0.006)}</Thickness><WeldType>DoubleFillet</WeldType>"
            "<ConnectedPartIds><string>11</string><string>2</string></ConnectedPartIds>"
            f"<Start><Id>0</Id><Name /><X>{_f(L['e'])}</X>"
            f"<Y>{_f(L['yb'] + L['tb']/2)}</Y><Z>{_f(zw0)}</Z></Start>"
            f"<End><Id>0</Id><Name /><X>{_f(L['e'])}</X>"
            f"<Y>{_f(L['yb'] + L['tb']/2)}</Y><Z>{_f(L['h'])}</Z></End>"
            "</WeldData></Welds>")

        connection_xml = (
            "<Connections><ConnectionData>"
            "<ConenctionPointId>1</ConenctionPointId>"
            f"{beams_xml}{plates_xml}<FoldedPlates />{bolts_xml}"
            f"<AnchorGrids />{welds_xml}<ConcreteBlocks /><CutBeamByBeams />"
            "</ConnectionData></Connections>")

        # ---- assemble -----------------------------------------------------
        points_xml = "".join(_point(pid, x, y, z, nm)
                             for pid, x, y, z, nm in self.points)
        return (
            "<OpenModel>"
            "<Version>2</Version>"
            "<OriginSettings>"
            f"<ProjectName>EccentricBoltedShearConnection</ProjectName>"
            f"<ProjectDescription>{escape(ci.title)}</ProjectDescription>"
            "<Author>EccentricBolt GUI</Author>"
            "<DateOfCreate>0001-01-01T00:00:00</DateOfCreate>"
            "<CrossSectionConversionTable>SCIA</CrossSectionConversionTable>"
            f"<CountryCode>{country}</CountryCode>"
            "<ImportRecommendedWelds>false</ImportRecommendedWelds>"
            "<CheckEquilibrium>true</CheckEquilibrium>"
            "</OriginSettings>"
            f"<Point3D>{points_xml}</Point3D>"
            f"<LineSegment3D>{''.join(seg_xml)}</LineSegment3D>"
            "<ArcSegment3D /><PolyLine3D /><Region3D />"
            "<MatConcrete /><MatReinforcement />"
            f"<MatSteel>{mats_xml}</MatSteel>"
            "<MatPrestressSteel /><MatWelding />"
            f"<CrossSection>{''.join(css_xml)}</CrossSection>"
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
    def build(self, plate_geometry: str = "region2d") -> str:
        """The OpenModelContainer document (UTF-8 declaration)."""
        return ('<?xml version="1.0" encoding="utf-8"?>\r\n'
                '<OpenModelContainer xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\r\n'
                + self._open_model(plate_geometry) + "\r\n"
                "<OpenModelResult />\r\n"
                "</OpenModelContainer>\r\n")

    # ----------------------------------------------------------------------
    def variants(self):
        """(label, xml_text) pairs, the format the Connection REST API
        documents first: an OpenModelContainer with a Region2D plate outline.
        The remaining entries are compatibility fallbacks (SVG plate region,
        UTF-16 encoding, bare OpenModel) should a service revision reject the
        preferred combination."""
        return [
            ("container-region2d", self.build(plate_geometry="region2d")),
            ("container-region", self.build(plate_geometry="region")),
            ("utf16-container-region2d",
             self.build(plate_geometry="region2d").replace(
                 'encoding="utf-8"', 'encoding="utf-16"')),
            ("openmodel-region2d", self._bare_open_model()),
        ]

    def _bare_open_model(self) -> str:
        # fallback for a service that sniffs a bare OpenModel file: the
        # namespace declarations live on the container otherwise
        inner = self._open_model("region2d").replace(
            "<OpenModel>",
            '<OpenModel xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">', 1)
        return '<?xml version="1.0" encoding="utf-8"?>\r\n' + inner + "\r\n"

    def build_bytes(self, variant: str = "container-region2d") -> bytes:
        xml = dict((k, v) for k, v in self.variants())[variant]
        enc = "utf-16" if variant.startswith("utf16") else "utf-8"
        return xml.encode(enc)

    def write(self, path: str, variant: str = "container-region2d") -> str:
        with open(path, "wb") as fh:
            fh.write(self.build_bytes(variant))
        return path


def member_load_components(ci: ConnectionInput):
    """Internal force components (N, Vy, Vz, Mx, My, Mz) in the local systems of
    member A (support plate) and member C (load stub) that represent the
    eccentric load at the connection node.

    Both members are vertical with the IOM ``CoordSystemByPoint``/ZX local
    system used in :mod:`eccbolt.iom`, i.e.  x_local = +Z(global),
    y_local = -Y(global), z_local = +X(global).
    """
    P = ci.load.P
    t = math.radians(ci.load.theta)
    px, pz = P * math.sin(t), -P * math.cos(t)      # global X, Z
    h = ci.load.h_load if ci.load.h_load is not None else (
        ci.plate_b.cy + ci.plate_b.height / 2)
    my_g = P * (ci.load.e * math.cos(t) + h * math.sin(t))   # about +Y global
    # member C carries the load, member A the reaction
    c = dict(n=pz, vy=0.0, vz=px, mx=0.0, my=-my_g, mz=0.0)
    a = dict(n=-pz, vy=0.0, vz=-px, mx=0.0, my=my_g, mz=0.0)
    return a, c
