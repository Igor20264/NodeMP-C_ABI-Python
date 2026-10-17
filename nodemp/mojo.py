"""Mojo on the pinned core.

Mojo is not a second interpreter inside the server. CPython stays the language
host. A Mojo program is compiled to a Python extension (``PyInit_<name>``) or
to a C ABI shared library, and calls into it are queued on the core lane.
``ctypes`` releases the GIL around the native call, so that core runs Mojo
while the server worker keeps taking callbacks.

Build an extension:

    mojo build kernel.mojo --emit shared-lib -o kernel.pyd

The extension initializes the Mojo runtime itself. A raw C ABI library that
uses the Mojo standard library should export ``initialize_runtime`` as
``abi("C")``; the loader calls that symbol on the lane once, if it is present.
"""

from __future__ import annotations

import importlib.util
import os
import sys

from nodemp.runtime import native_allowed


def _require_native() -> None:
    if not native_allowed():
        raise PermissionError("mojo is available to trusted python resources")


def load_extension(path: str):
    """Load a Mojo (or C) Python extension. ``PyInit_<stem>`` must exist."""
    _require_native()
    full = os.path.abspath(path)
    name = os.path.splitext(os.path.basename(full))[0]
    loader = importlib.util.ExtensionFileLoader(name, full)
    spec = importlib.util.spec_from_file_location(name, full, loader=loader)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load Mojo extension {full}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def enable_importer():
    """Turn on ``mojo.importer`` when the Mojo SDK is on ``PYTHONPATH``.

    The importer shells out to ``mojo build --emit shared-lib``. That compiler
    is optional and is not shipped inside the host.
    """
    _require_native()
    import mojo.importer  # noqa: F401

    return mojo.importer


def load_native(path: str) -> int:
    """Load a shared library and, if it exports ``initialize_runtime``, call it on the lane."""
    _require_native()
    import _nodemp

    return int(_nodemp.mojo_load(os.path.abspath(path)))


def symbol(handle: int, name: str) -> int:
    _require_native()
    import _nodemp

    addr = int(_nodemp.mojo_symbol(int(handle), name) or 0)
    if not addr:
        raise AttributeError(name)
    return addr


def invoke(handle: int, name: str, restype, argtypes, args=(), done=None):
    """Call ``name`` on the pinned core. ``done(result, error)`` runs on the worker."""
    import ctypes

    from nodemp.api import node

    addr = symbol(handle, name)
    fn = ctypes.CFUNCTYPE(restype, *argtypes)(addr)
    values = tuple(args)

    def run(_fn=fn, _values=values):
        return _fn(*_values)

    run._keep = fn
    node.job(run, done=done)


def close(handle: int) -> None:
    _require_native()
    import _nodemp

    _nodemp.mojo_close(int(handle))
