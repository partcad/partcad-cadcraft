#
# PartCAD, 2026
#
# Licensed under Apache License, Version 2.0.
#
import importlib.util
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)


def plugin_module(name):
    """One of the plugin's scripts, loaded from its file as PartCAD's wrappers load it.

    Under a name of its own, so that a module of the same name elsewhere on the
    path -- PartCAD has a 'drawing' or two -- is never the one imported. The
    scripts import their siblings by bare name, which is why the plugin's
    directory is put on the path as well.
    """
    import sys

    if PLUGIN not in sys.path:
        sys.path.insert(0, PLUGIN)
    spec = importlib.util.spec_from_file_location("cadcraft_plugin_" + name, os.path.join(PLUGIN, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def cadcraft():
    """A 'cadcraft-cli' to run, fetched as the plugin fetches it; skips where there can be none."""
    tool = plugin_module("cadcraft_tool")
    try:
        return tool.executable()
    except tool.CadcraftError as e:
        pytest.skip("CADCraft is not available here: %s" % e)
