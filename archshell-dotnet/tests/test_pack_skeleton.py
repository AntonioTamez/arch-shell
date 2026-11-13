from importlib.metadata import requires

import archshell_dotnet


def test_pack_imports_with_version():
    assert archshell_dotnet.__version__ == "0.1.0"


def test_pack_depends_on_core():
    assert any(req.split(";")[0].strip() == "archshell" for req in requires("archshell-dotnet"))
