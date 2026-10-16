"""Tests of the IOM (IDEA Open Model) XML export.

The document format is pinned to what the Connection REST API service actually
accepts (see the module docstring of :mod:`eccbolt.iom`): an
``OpenModelContainer`` with ``<Version>2</Version>`` and the version-2 dialect
of the connection data, in the exact member order of ``XmlSerializer``.
"""
import os
import sys
import xml.etree.ElementTree as ET

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eccbolt.iom import IomBuilder, member_load_components, parse_open_model
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
        enc = "utf-16" if label.startswith("utf16") else "utf-8"
        blob = b.build_bytes(label)
        assert blob.decode(enc).lstrip("\ufeff").startswith("<?xml")


def test_document_wrapper_and_version():
    """The service imports an ``OpenModelContainer`` and upgrades it from the
    version-2 dialect; a bare ``OpenModel`` root or a made-up (hybrid) version
    string is rejected with HTTP 500."""
    xml = IomBuilder(crawford_kulak_example()).build()
    assert "<OpenModelContainer" in xml
    assert "<OpenModelResult" in xml
    root = parse_open_model(xml)
    assert root.tag == "OpenModel"
    assert root.find("Version").text == "2"


def test_verified_dialect_spellings():
    """Spellings verified against the official import fixtures shipped with the
    Connection API SDK (they are fed to a live ``import-iom-file`` endpoint)."""
    xml = IomBuilder(crawford_kulak_example()).build()
    assert "<BoltInteraction>Interaction</BoltInteraction>" in xml
    assert "<CrossSectionType>Rect</CrossSectionType>" in xml
    assert "<IsInPrincipal>false</IsInPrincipal>" in xml
    assert "<CountryCode>American</CountryCode>" in xml
    assert "<WeldType>DoubleFillet</WeldType>" in xml


def test_element_order_matches_dotnet_serializer():
    """``XmlSerializer`` only assigns members in declaration order - anything
    out of order is silently dropped by the .NET deserializer."""
    root = parse_open_model(IomBuilder(crawford_kulak_example()).build())
    bg = root.find("Connections/ConnectionData/BoltGrids/BoltGrid")
    assert [c.tag for c in bg][:6] == ["Id", "IsAnchor", "AnchorLen",
                                       "HoleDiameter", "Diameter", "HeadDiameter"]
    assert [c.tag for c in bg][-3:] == ["ConnectedPartIds", "ShearInThread",
                                        "BoltInteraction"]
    assert [c.tag for c in bg.find("Origin")] == ["Id", "Name", "X", "Y", "Z"]
    weld = root.find("Connections/ConnectionData/Welds/WeldData")
    assert [c.tag for c in weld] == ["Id", "Thickness", "WeldType",
                                     "ConnectedPartIds", "Start", "End"]
    plate = root.find("Connections/ConnectionData/Plates/PlateData")
    assert [c.tag for c in plate] == ["Id", "Name", "Thickness", "Material",
                                      "OutlinePoints", "Origin", "AxisX", "AxisY",
                                      "AxisZ", "Geometry", "OriginalModelId",
                                      "IsNegativeObject"]
    el = root.find("Element1D/Element1D")
    assert [c.tag for c in el] == ["Id", "Name", "Segment", "RotationRx",
                                   "EccentricityBeginX", "EccentricityBeginY",
                                   "EccentricityBeginZ", "EccentricityEndX",
                                   "EccentricityEndY", "EccentricityEndZ"]
    mem = root.find("Member1D/Member1D")
    assert [c.tag for c in mem] == ["Id", "Name", "Elements1D", "Member1DType",
                                    "CrossSection", "Alignment", "MirrorY",
                                    "MirrorZ"]


def test_structure():
    ci, root = _root()
    assert root.find("OriginSettings/CountryCode").text == "American"
    data = root.find("Connections/ConnectionData")
    assert data.find("ConenctionPointId").text == "1"
    bg = data.find("BoltGrids/BoltGrid")
    assert len(bg.find("Positions")) == ci.n_bolts
    parts = [s.text for s in bg.find("ConnectedPartIds")]
    assert parts == ["1", "11"]                     # OriginalModelIds
    assert bg.find("BoltInteraction").text == "Interaction"
    assert bg.find("Material").text == ci.bolt_mat.designation
    assert bg.find("ShearInThread").text == \
        ("true" if ci.threads_in_shear_plane else "false")
    plates = data.find("Plates/PlateData")
    assert float(plates.find("Thickness").text) == ci.plate_b.thickness
    # material is referenced by name (upgraded to a reference by the service)
    assert plates.find("Material").text == ci.plate_b.material.designation
    assert root.find("MatSteel/MatSteel/Name").text == ci.plate_a.material.designation
    assert plates.find("Geometry/Outline") is not None
    beams = data.findall("Beams/BeamData")
    assert len(beams) == 2
    assert beams[0].find("IsBearingMember").text == "true"
    members = root.findall("Member1D/Member1D")
    assert len(members) == 2
    assert members[0].find("CrossSection/TypeName").text == "CrossSection"
    assert len(root.findall("Element1D/Element1D")) == 3
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


def test_load_components_equilibrium():
    ci = crawford_kulak_example()
    a, c = member_load_components(ci)
    for k in a:
        assert a[k] + c[k] == 0 or abs(a[k] + c[k]) < 1e-9
    # moment = P * e for a vertical load
    assert c["my"] == -(ci.load.P * ci.load.e)
