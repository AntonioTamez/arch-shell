from importlib.metadata import requires

import archshell_dotnet


def test_pack_imports_with_version():
    assert archshell_dotnet.__version__ == "0.1.0rc1"


def test_pack_depends_on_core_within_its_declared_range():
    # The pack's core range (AD-22, docs/compatibility.md).
    assert any(req.split(";")[0].strip() == "archshell>=0.1.0rc1,<0.2" for req in requires("archshell-dotnet"))


def test_pack_pins_its_analyzer_version():
    assert archshell_dotnet.ANALYZER_VERSION == "0.1.0-rc.1"
