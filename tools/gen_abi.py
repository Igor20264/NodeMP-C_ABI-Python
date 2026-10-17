"""Generate the ctypes image of node.h.

The server looks NodeApi up by byte offset, so the generated structure has to
carry every function pointer in the header's order. Run this again when
node.h gains a field:

    python tools/gen_abi.py
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "sdk" / "node.h"
OUT_PY = Path(__file__).resolve().parents[1] / "nodemp" / "_abi.py"
OUT_C = Path(__file__).resolve().parent / "abi_check.c"

CALLBACKS = {
    "NodeClientEventCallback": "CFUNCTYPE(None, c_int, c_char_p, c_void_p)",
    "NodeBuiltinEventCallback": "CFUNCTYPE(None, c_int, c_void_p)",
    "NodeVehicleEventCallback": "CFUNCTYPE(None, c_int, c_int, c_char_p, c_void_p)",
    "NodeCancellableEventCallback": "CFUNCTYPE(c_int, c_int, c_int, c_char_p, c_void_p, c_int, c_void_p)",
    "NodeTimerCallback": "CFUNCTYPE(None, c_int, c_void_p)",
    "NodeLogSink": "CFUNCTYPE(c_int, c_char_p, c_int, c_char_p, c_void_p)",
    "NodeCanRelayCallback": "CFUNCTYPE(c_int, c_int, c_int, c_int, c_int, c_int, c_void_p)",
    "NodeJobWorkFn": "CFUNCTYPE(c_void_p, c_void_p)",
    "NodeJobDoneFn": "CFUNCTYPE(None, c_void_p, c_void_p)",
    "NodeHttpCallback": "CFUNCTYPE(None, c_int, c_void_p, c_int, c_char_p, c_void_p)",
    "NodeResourceEventCallback": "CFUNCTYPE(None, c_char_p, c_char_p, c_void_p)",
    "NodeBusAnswerCallback": "CFUNCTYPE(None, c_char_p, c_char_p, c_void_p)",
    "NodeModuleChannelCallback": "CFUNCTYPE(None, c_int, c_uint32, c_void_p, c_int, c_void_p)",
}

STRUCTS = ("NodeVehicleTransform", "NodeVehicleControls", "NodeVehicleInfo")

STRUCT_CTYPES = {
    "float": "c_float",
    "int": "c_int",
    "double": "c_double",
    "char": "c_char",
    "uint32_t": "c_uint32",
    "uint16_t": "c_uint16",
    "uint8_t": "c_uint8",
    "int64_t": "c_int64",
}

RET_CTYPES = {
    "void": "None",
    "int": "c_int",
    "int64_t": "c_int64",
    "double": "c_double",
    "uint32_t": "c_uint32",
}

MANUAL_KINDS = {
    "callback",
    "struct",
    "host",
    "farr",
    "blob",
    "iptr",
    "uptr",
    "ptr",
    "hex",
}


def strip_comments(text: str) -> str:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise SystemExit("unclosed block comment in node.h")
            out.append(" ")
            i = j + 2
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            if j < 0:
                break
            out.append("\n")
            i = j
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def extract_block(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i < 0:
        raise SystemExit(f"missing {start}")
    j = text.find(end, i)
    if j < 0:
        raise SystemExit(f"missing {end}")
    return text[i + len(start) : j]


def split_params(args: str) -> list[str]:
    args = args.strip()
    if not args or args == "void":
        return []
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in args:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        parts.append(tail)
    return parts


def parse_param(raw: str) -> dict:
    text = re.sub(r"\s+", " ", raw).strip()
    array = None
    match = re.search(r"\[(\d+)\]$", text)
    if match:
        array = int(match.group(1))
        text = text[: match.start()].strip()
    pieces = text.rsplit(" ", 1)
    if len(pieces) != 2:
        raise SystemExit(f"cannot parse parameter: {raw}")
    typ, name = pieces[0].strip(), pieces[1].strip()
    bare = re.sub(r"\s+", "", typ.replace("const ", ""))
    kind, ctype = map_type(bare, name, array)
    return {"name": name, "kind": kind, "ctype": ctype}


def map_type(bare: str, name: str, array: int | None) -> tuple[str, str]:
    if bare in CALLBACKS:
        return "callback", CALLBACKS[bare]
    if bare in ("NodeVehicleInfo*", "NodeVehicleTransform*", "NodeVehicleControls*"):
        return "struct", f"POINTER({bare[:-1]})"
    if bare == "NodeLanguageHost*":
        return "host", "c_void_p"
    if array and bare == "char":
        return "hex", "c_char_p"
    if array and bare == "float":
        return "farr", "POINTER(c_float)"
    if bare in ("char*", "char"):
        if name in ("buf", "out"):
            return "buf", "c_char_p"
        return "cstr", "c_char_p"
    if bare == "uint8_t*":
        return "blob", "POINTER(c_uint8)"
    if bare == "int*":
        return "iptr", "POINTER(c_int)"
    if bare == "uint32_t*":
        return "uptr", "POINTER(c_uint32)"
    if bare == "void*":
        return "ptr", "c_void_p"
    if bare == "int":
        if name == "buflen":
            return "buflen", "c_int"
        return "int", "c_int"
    if bare == "uint32_t":
        return "u32", "c_uint32"
    if bare == "int64_t":
        return "i64", "c_int64"
    if bare == "double":
        return "double", "c_double"
    if bare == "float":
        return "float", "c_float"
    raise SystemExit(f"unknown parameter type {bare} ({name})")


def classify(params: list[dict]) -> str:
    kinds = [p["kind"] for p in params]
    if any(k in MANUAL_KINDS for k in kinds):
        return "manual"
    if len(kinds) >= 2 and kinds[-2:] == ["buf", "buflen"]:
        return "text"
    return "plain"


def parse_functions(body: str) -> list[dict]:
    found = re.findall(
        r"([A-Za-z_][\w\s\*]*?)\s*\(\s*\*(\w+)\s*\)\s*\((.*?)\)\s*;",
        body,
        flags=re.S,
    )
    funcs = []
    for ret, name, args in found:
        ret = re.sub(r"\s+", " ", ret).strip()
        if ret not in RET_CTYPES:
            raise SystemExit(f"{name}: unknown return type {ret}")
        params = [parse_param(p) for p in split_params(args)]
        funcs.append(
            {
                "name": name,
                "ret": ret,
                "ret_ctype": RET_CTYPES[ret],
                "params": params,
                "auto": classify(params),
            }
        )
    if not funcs:
        raise SystemExit("no NodeApi functions parsed")
    names = [f["name"] for f in funcs]
    if len(names) != len(set(names)):
        raise SystemExit("duplicate NodeApi field")
    if names[0] != "log_info" or names[-1] != "set_queue_limits_json":
        raise SystemExit(f"unexpected NodeApi ends: {names[0]} .. {names[-1]}")
    return funcs


def parse_struct(text: str, name: str) -> list[tuple[str, str]]:
    body = extract_block(text, f"typedef struct {name} {{", f"}} {name};")
    fields = []
    for match in re.finditer(
        r"([A-Za-z_][\w]*)\s+([A-Za-z_]\w*)(?:\[(\d+)\])?\s*;",
        body,
    ):
        ctype_name, field, count = match.group(1), match.group(2), match.group(3)
        if ctype_name not in STRUCT_CTYPES:
            raise SystemExit(f"{name}.{field}: unknown {ctype_name}")
        ctype = STRUCT_CTYPES[ctype_name]
        if count:
            ctype = f"{ctype} * {count}"
        fields.append((field, ctype))
    if not fields:
        raise SystemExit(f"no fields in {name}")
    return fields


def parse_constants(text: str) -> list[tuple[str, int]]:
    consts = []
    for name, value in re.findall(r"(?m)^#define (NODE_[A-Z0-9_]+) (\d+)u?(?:\s|$)", text):
        consts.append((name, int(value)))
    return consts


def parse_header(text: str) -> dict:
    stripped = strip_comments(text)
    major = re.search(r"#define NODE_ABI_VERSION_MAJOR (\d+)u?", text)
    minor = re.search(r"#define NODE_ABI_VERSION_MINOR (\d+)u?", text)
    if not major or not minor:
        raise SystemExit("ABI version macros missing")
    return {
        "major": int(major.group(1)),
        "minor": int(minor.group(1)),
        "functions": parse_functions(extract_block(stripped, "typedef struct NodeApi {", "} NodeApi;")),
        "structs": {name: parse_struct(stripped, name) for name in STRUCTS},
        "constants": parse_constants(text),
    }


def render_python(model: dict) -> str:
    lines = [
        '"""Ctypes view of NodeApi. Generated by tools/gen_abi.py — do not edit."""',
        "",
        "from ctypes import (",
        "    CFUNCTYPE,",
        "    POINTER,",
        "    Structure,",
        "    c_char,",
        "    c_char_p,",
        "    c_double,",
        "    c_float,",
        "    c_int,",
        "    c_int64,",
        "    c_uint8,",
        "    c_uint16,",
        "    c_uint32,",
        "    c_void_p,",
        ")",
        "",
        f"ABI_MAJOR = {model['major']}",
        f"ABI_MINOR = {model['minor']}",
        "ABI_VERSION = (ABI_MAJOR << 16) | ABI_MINOR",
        "",
        "CONST = {",
    ]
    for name, value in model["constants"]:
        lines.append(f'    "{name}": {value},')
    lines.append("}")
    lines.append("")
    for name, expr in CALLBACKS.items():
        lines.append(f"{name} = {expr}")
    lines.append("")
    for struct, fields in model["structs"].items():
        lines.append(f"class {struct}(Structure):")
        lines.append("    _fields_ = [")
        for field, ctype in fields:
            lines.append(f'        ("{field}", {ctype}),')
        lines.append("    ]")
        lines.append("")
    lines.append("class NodeApi(Structure):")
    lines.append("    _fields_ = [")
    lines.append('        ("abi_version", c_uint32),')
    lines.append('        ("struct_size", c_uint32),')
    for fn in model["functions"]:
        args = ", ".join(p["ctype"] for p in fn["params"])
        prefix = ", " if args else ""
        lines.append(
            f'        ("{fn["name"]}", CFUNCTYPE({fn["ret_ctype"]}{prefix}{args})),'
        )
    lines.append("    ]")
    lines.append("")
    lines.append("CATALOG = {")
    for fn in model["functions"]:
        kinds = ", ".join(f'"{p["kind"]}"' for p in fn["params"])
        params = ", ".join(f'"{p["name"]}"' for p in fn["params"])
        lines.append(
            f'    "{fn["name"]}": {{"ret": "{fn["ret"]}", "auto": "{fn["auto"]}", '
            f'"params": ({params}{"," if len(fn["params"]) == 1 else ""}), '
            f'"kinds": ({kinds}{"," if len(fn["params"]) == 1 else ""}),}},'
        )
    lines.append("}")
    lines.append("")
    lines.append("for _name, _spec in CATALOG.items():")
    lines.append('    _spec["offset"] = getattr(NodeApi, _name).offset')
    lines.append("")
    lines.append("FUNCTION_ORDER = tuple(CATALOG)")
    lines.append("")
    return "\n".join(lines)


def render_c(model: dict) -> str:
    lines = [
        "/* Generated by tools/gen_abi.py. Prints the compiler's view of node.h. */",
        "#include \"node.h\"",
        "#include <stddef.h>",
        "#include <stdio.h>",
        "",
        "int main(void) {",
        '    printf("sizeof NodeApi %zu\\n", sizeof(NodeApi));',
        '    printf("NodeApi.abi_version %zu\\n", offsetof(NodeApi, abi_version));',
        '    printf("NodeApi.struct_size %zu\\n", offsetof(NodeApi, struct_size));',
    ]
    for fn in model["functions"]:
        lines.append(
            f'    printf("NodeApi.{fn["name"]} %zu\\n", offsetof(NodeApi, {fn["name"]}));'
        )
    for struct, fields in model["structs"].items():
        lines.append(f'    printf("sizeof {struct} %zu\\n", sizeof({struct}));')
        for field, _ctype in fields:
            lines.append(
                f'    printf("{struct}.{field} %zu\\n", offsetof({struct}, {field}));'
            )
    lines.append("    return 0;")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    model = parse_header(HEADER.read_text(encoding="utf-8"))
    OUT_PY.write_text(render_python(model), encoding="utf-8", newline="\n")
    OUT_C.write_text(render_c(model), encoding="utf-8", newline="\n")
    autos: dict[str, int] = {}
    for fn in model["functions"]:
        autos[fn["auto"]] = autos.get(fn["auto"], 0) + 1
    print(
        f"ABI {model['major']}.{model['minor']}: {len(model['functions'])} functions {autos}"
    )


if __name__ == "__main__":
    main()
