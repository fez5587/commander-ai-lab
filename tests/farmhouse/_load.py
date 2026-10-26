"""Load a farmhouse module by file path. (Putting services/ on sys.path would shadow the standard library's logging with
services/logging.py, so the tests never do that.)"""
import importlib.util
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
