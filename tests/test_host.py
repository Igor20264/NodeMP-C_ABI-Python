"""Layout of the generated NodeApi image, and the untrusted-path rule."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from gen_abi import HEADER, parse_header  # noqa: E402
from nodemp._abi import CATALOG, CONST, FUNCTION_ORDER, NodeApi, NodeVehicleControls, NodeVehicleInfo, NodeVehicleTransform  # noqa: E402
from nodemp.api import BUILTIN, REQUESTS, VEHICLE, Node, _prepare, _write_reason, node  # noqa: E402
from nodemp.runtime import install_system_hook, path_allowed, pip_argv
from nodemp.state import Resource, current  # noqa: E402

HEADER_TEXT = HEADER.read_text(encoding="utf-8")
MODEL = parse_header(HEADER_TEXT)

SKIP_MANUAL = {
    "register_language_host",
    "register_client_event",
    "unregister_client_event",
    "register_builtin_event",
    "unregister_builtin_event",
    "register_cancellable_event",
    "unregister_cancellable_event",
    "register_vehicle_event",
    "unregister_vehicle_event",
    "register_relay_filter",
    "unregister_relay_filter",
    "register_resource_event",
    "unregister_resource_event",
    "register_module_channel",
    "unregister_module_channel",
    "call_resource_event",
}


def test_catalog_matches_header():
    names = [fn["name"] for fn in MODEL["functions"]]
    assert FUNCTION_ORDER == tuple(names)
    assert names[0] == "log_info"
    assert names[-1] == "set_queue_limits_json"
    for fn in MODEL["functions"]:
        assert CATALOG[fn["name"]]["auto"] == fn["auto"]


def test_pointer_image_is_packed_after_the_abi_words():
    assert NodeApi.abi_version.offset == 0
    assert NodeApi.struct_size.offset == 4
    for index, name in enumerate(FUNCTION_ORDER):
        assert getattr(NodeApi, name).offset == 8 + index * 8
        assert CATALOG[name]["offset"] == 8 + index * 8
    assert ctypes.sizeof(NodeApi) == 8 + len(FUNCTION_ORDER) * 8


def test_constants_cover_the_header():
    assert CONST["NODE_ABI_VERSION_MAJOR"] == 2
    assert CONST["NODE_ABI_VERSION_MINOR"] == 8
    assert CONST["NODE_LOCK_ALL"] == 1
    assert CONST["NODE_VERIFY_STRICT"] == 4
    assert node.NODE_ROLE_DRIVER == 1


def test_manual_calls_are_wrapped():
    for name, spec in CATALOG.items():
        if spec["auto"] != "manual" or name in SKIP_MANUAL:
            continue
        assert callable(getattr(Node, name)), name


def test_event_routing_names_do_not_overlap_requests():
    assert REQUESTS.isdisjoint(VEHICLE)
    assert REQUESTS.isdisjoint(BUILTIN)
    assert "playerJoined" in BUILTIN
    assert "playerLeft" in VEHICLE
    assert "vehicleSpawnRequest" in REQUESTS


def test_reason_buffer_is_written_in_place():
    buf = ctypes.create_string_buffer(16)
    _write_reason(ctypes.addressof(buf), 16, "no buses")
    assert buf.value == b"no buses"


def test_null_pointer_arguments_are_accepted():
    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_uint8))
    def probe(pointer):
        return 0 if not pointer else 1

    assert probe(None) == 0


def test_path_rule_rejects_a_sibling_prefix():
    root = Path("C:/game/resource") if sys.platform == "win32" else Path("/game/resource")
    inside = root / "server" / "main.py"
    sibling = Path(str(root) + "-evil") / "secret"
    assert path_allowed(inside, [root])
    assert not path_allowed(sibling, [root])
    assert path_allowed(root, [root])


def test_cstr_arguments_are_encoded():
    assert _prepare("log_info", ("hi",)) == (b"hi",)
    assert _prepare("emit_client", (3, "greeting", "Welcome")) == (3, b"greeting", b"Welcome")
    assert _prepare("get_vehicle_tag", (1, "color")) == (1, b"color")
    assert _prepare("set_resource_owner", (None,)) == (None,)
    assert _prepare("set_resource_owner", (b"python-gate",)) == (b"python-gate",)


def test_plain_pip_command_stays_inside_the_host():
    assert pip_argv("pip install requests") == ["install", "requests"]
    assert pip_argv("pip install foo==1.2.3") == ["install", "foo==1.2.3"]
    assert pip_argv('py -3.15 -m pip install "foo bar"') == ["install", "foo bar"]
    assert pip_argv("python -m pip install requests") == ["install", "requests"]
    assert pip_argv("pip install requests && calc") is None
    assert pip_argv("echo pip install requests") is None
    assert pip_argv(None) is None


def test_untrusted_pip_stays_closed():
    res = Resource("demo", "C:/game/demo")
    res.trusted = False
    token = current.set(res)
    original = os.system
    try:
        install_system_hook()
        try:
            os.system("pip install requests")
        except PermissionError:
            return
        raise AssertionError("untrusted pip was allowed")
    finally:
        os.system = original
        current.reset(token)


def load_tests(loader, tests, pattern):
    import unittest

    suite = unittest.TestSuite()
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            suite.addTest(unittest.FunctionTestCase(obj, description=name))
    return suite


def test_compiler_layout_when_present():
    exe = ROOT / "build" / "abi_check.exe"
    if not exe.is_file():
        return
    text = subprocess.check_output([str(exe)], text=True)
    values = {}
    for line in text.splitlines():
        name, offset = line.rsplit(" ", 1)
        values[name] = int(offset)
    assert values["sizeof NodeApi"] == ctypes.sizeof(NodeApi)
    assert values["sizeof NodeVehicleInfo"] == ctypes.sizeof(NodeVehicleInfo)
    assert values["sizeof NodeVehicleTransform"] == ctypes.sizeof(NodeVehicleTransform)
    assert values["sizeof NodeVehicleControls"] == ctypes.sizeof(NodeVehicleControls)
    for name in ("abi_version", "struct_size", *FUNCTION_ORDER):
        assert values[f"NodeApi.{name}"] == getattr(NodeApi, name).offset
    for struct in (NodeVehicleInfo, NodeVehicleTransform, NodeVehicleControls):
        for field, _ctype in struct._fields_:
            assert values[f"{struct.__name__}.{field}"] == getattr(struct, field).offset


if __name__ == "__main__":
    import unittest

    unittest.main(verbosity=2)
