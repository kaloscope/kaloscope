import importlib

# import node classes from submodules
importlib.import_module(f"{__name__}.start")
importlib.import_module(f"{__name__}.end")
importlib.import_module(f"{__name__}.metadata")
importlib.import_module(f"{__name__}.general")
importlib.import_module(f"{__name__}.control")
