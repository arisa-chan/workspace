"""Tests of the IOM (IDEA Open Model) XML export.

The document format is pinned to what the installed Connection REST API service
actually accepts (see the module docstring of :mod:`eccbolt.iom`): an
``OpenModelContainer`` in the current ``IdeaRS.OpenModel`` dialect
(``<Version>3.2.0</Version>``) with the elements in the exact declaration order
of the .NET ``XmlSerializer``.
"""
import os
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eccbolt.iom import IOM_VERSION, IomBuilder, member_load_components, parse_open_model
from eccbolt.model import INCH, KIP, LoadSpec
from eccbolt.presets import crawford_kulak_example


def _root():
    ci = crawford_kulak_example()
    xml = IomBuilder(ci).build()
    return ci, parse_open_model(xml)


def test_xml_wellformed_all_variants(tmp_path):
    ci = crawford_kulak_example()
    b = IomBuilder(ci)
    # default file: the OpenModelContainer document the Connection API imports
    p = b.write(str(tmp_path / "m.xml"))
    raw = open(p, "rb").read()
    assert raw.startswith(b"<?xml version=\"1.0\" encoding=\"utf-8\"?>")
    doc = ET.fromstring(raw.decode("utf-8"))
    assert doc.tag == "OpenModelContainer"
    assert doc.find("OpenModel") is not None
    # every compatibility variant parses and carries the right encoding
    for label, xml in b.variants():
        ET.fromstring(xml)
        enc = "utf-16" if label.endswith("utf16") else "utf-8"
        blob = b.build_bytes(label)
        assert blob.decode(enc).lstrip("\ufeff").startswith("<?xml")


def test_document_wrapper_and_version():
    """The service imports an ``OpenModelContainer``; the dialect must be the
    one the installed service writes (it exports 3.2.0 on 26.0)."""
    xml = IomBuilder(crawford_kulak_example()).build()
    assert "<OpenModelContainer" in xml
    assert "<OpenModelResult" in xml
    root = parse_open_model(xml)
    assert root.tag == "OpenModel"
    assert root.find("Version").text == IOM_VERSION


def test_verified_dialect_spellings():
    """Spellings verified against a live ``import-iom-file`` endpoint."""
    xml = IomBuilder(crawford_kulak_example()).build()
    assert "<BoltInteraction>Interaction</BoltInteraction>" in xml
    assert "<CrossSectionType>Rect</CrossSectionType>" in xml
    assert "<IsInPrincipal>false</IsInPrincipal>" in xml
    assert "<CountryCode>American</CountryCode>" in xml
    # the bolt assembly must be the *list* property with the assembly nested
    assert "<BoltAssembly><BoltAssembly>" in xml
    assert "<Code>American</Code>" in xml       # bolt grade code = CountryCode


def test_element_order_matches_dotnet_serializer():
    """``XmlSerializer`` only assigns members in declaration order - anything
    out of order is silently dropped by the .NET deserializer."""
    root = parse_open_model(IomBuilder(crawford_kulak_example()).build())
    bg = root.find("Connections/ConnectionData/BoltGrids/BoltGrid")
    assert [c.tag for c in bg] == [
        "Id", "Origin", "AxisX", "AxisY", "AxisZ", "Positions",
        "ConnectedParts", "Length", "ShearInThread", "BoltInteraction",
        "BoltAssembly"]
    assert [c.tag for c in bg.find("Origin")] == ["Id", "X", "Y", "Z"]
    el = root.find("Element1D/Element1D")
    assert [c.tag for c in el] == [
        "Id", "Name", "CrossSectionBegin", "CrossSectionEnd", "Segment",
        "RotationRx", "EccentricityBegin", "EccentricityEnd"]
    mem = root.find("Member1D/Member1D")
    assert [c.tag for c in mem] == [
        "Id", "Name", "Elements1D", "Member1DType", "Alignment", "MirrorY",
        "MirrorZ", "EccentricityBegin", "EccentricityEnd", "InsertionPoint",
        "EccentricityReference"]
    asm = root.find("BoltAssembly/BoltAssembly")
    assert [c.tag for c in asm][:3] == ["Id", "Name", "BoltGrade"]


def test_structure():
    ci, root = _root()
    assert root.find("OriginSettings/CountryCode").text == "American"
    data = root.find("Connections/ConnectionData")
    assert data.find("ConnectionPoint/TypeName").text == "ConnectionPoint"
    bg = data.find("BoltGrids/BoltGrid")
    assert len(bg.find("Positions")) == ci.n_bolts
    parts = [(r.find("TypeName").text, r.find("Id").text)
             for r in bg.find("ConnectedParts")]
    assert parts == [("BeamData", "1"), ("BeamData", "2")]   # the two members
    assert bg.find("BoltInteraction").text == "Interaction"
    assert bg.find("ShearInThread").text == \
        ("true" if ci.threads_in_shear_plane else "false")
    asm = root.find("BoltAssembly/BoltAssembly")
    assert float(asm.find("Diameter").text) == pytest.approx(ci.bolt_diameter)
    grade = root.find("MatBoltGrade/MaterialBoltGrade")
    assert grade.find("Name").text == ci.bolt_mat.designation
    assert float(grade.find("fub").text) == pytest.approx(ci.bolt_mat.fub)
    assert root.find("MatSteel/MatSteel/Name").text == ci.plate_a.material.designation
    beams = data.findall("Beams/BeamData")
    assert len(beams) == 2
    assert beams[0].find("IsBearingMember").text == "true"
    members = root.findall("Member1D/Member1D")
    assert len(members) == 2
    assert len(root.findall("Element1D/Element1D")) == 4
    cp = root.find("ConnectionPoint/ConnectionPoint")
    assert cp.find("Node/TypeName").text == "Point3D"
    assert len(cp.findall("ConnectedMembers/ConnectedMember")) == 2


def test_bolt_coordinates_roundtrip():
    ci, root = _root()
    bg = root.find("Connections/ConnectionData/BoltGrids/BoltGrid")
    got = [(float(p.find("X").text), float(p.find("Z").text))
           for p in bg.find("Positions")]
    assert got == [(pytest.approx(x), pytest.approx(y))
                   for x, y in ci.bolts_xy]


def test_bolt_group_and_plates():
    """The bolt group must be written so that the service turns it into a real
    ``ConnectorGridData`` operation (bolts + holes in the model), and both
    plates must be modelled face to face."""
    ci, root = _root()
    data = root.find("Connections/ConnectionData")
    bg = data.find("BoltGrids/BoltGrid")
    assert len(bg.find("Positions")) == ci.n_bolts
    parts = [(r.find("TypeName").text, r.find("Id").text)
             for r in bg.find("ConnectedParts")]
    assert parts == [("BeamData", "1"), ("BeamData", "2")]   # the two members
    # the grid origin lies inside the first plate so the service keeps it
    ta = ci.plate_a.thickness
    y0 = float(bg.find("Origin").find("Y").text)
    assert -ta / 2 <= y0 <= ta / 2
    asm = root.find("BoltAssembly/BoltAssembly")
    assert float(asm.find("Diameter").text) == pytest.approx(ci.bolt_diameter)
    assert [c.tag for c in asm][:3] == ["Id", "Name", "BoltGrade"]
    # the loaded plate's member is offset by the two half thicknesses
    pts = {p.find("Name").text: p for p in root.findall("Point3D/Point3D")
           if p.find("Name") is not None and p.find("Name").text}
    assert float(pts["A_top"].find("Y").text) == pytest.approx(0.0)
    assert float(pts["B_top"].find("Y").text) == pytest.approx(
        (ci.plate_a.thickness + ci.plate_b.thickness) / 2)


def test_load_components_equilibrium():
    ci = crawford_kulak_example()
    a, c = member_load_components(ci)
    for k in a:
        assert a[k] + c[k] == 0 or abs(a[k] + c[k]) < 1e-9
    # member local system x=+Z, y=-Y, z=+X: a vertical load with the lever arm
    # e produces an axial force and a moment about the local y axis
    assert c["n"] == pytest.approx(-ci.load.P)
    assert c["my"] == pytest.approx(-(ci.load.P * ci.load.e))
    assert c["mz"] == pytest.approx(0.0)
