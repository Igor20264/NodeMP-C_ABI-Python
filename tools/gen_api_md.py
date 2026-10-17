"""Write API.md from sdk/node.h.

    py -3.14 tools/gen_api_md.py
    py -3.14 tools/gen_api_md.py --host micropython
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gen_abi import HEADER, parse_header  # noqa: E402

SERVER_ABI = (2, 7)

PYTHON_EXTRA = [
    ("on", "name, fn", "Handle", "Событие по имени. Запрос, vehicle, builtin или клиентское."),
    ("on_client", "name, fn", "Handle", "Клиентское событие. Аргументы: player_id, data."),
    ("on_builtin", "name, fn", "Handle", "Уведомление с одним id."),
    ("on_vehicle", "name, fn", "Handle", "Событие с player_id, global_id, data."),
    ("on_request", "name, fn", "Handle", "Вердикт. False или строка запрещают."),
    ("on_resource", "name, fn", "Handle", "Событие шины ресурсов."),
    ("on_channel", "channel, fn", "Handle", "Канал модуля."),
    ("off", "handle", "None", "Снять подписку. Для таймера отменяет его."),
    ("set_timeout", "ms, fn", "Handle", "Один вызов. В функцию приходит timer_id."),
    ("set_interval", "ms, fn", "Handle", "Повтор. В функцию приходит timer_id."),
    ("after", "ms, fn", "Handle", "То же, что set_timeout."),
    ("every", "ms, fn", "Handle", "То же, что set_interval."),
    ("cancel", "handle", "None", "Остановить таймер."),
    ("job", "fn, *args, done=None", "None", "fn на закреплённом ядре. done(result, error) на worker."),
    ("submit_job", "fn, *args, done=None", "Handle", "Пул сервера, без привязки к ядру."),
    ("core_info", "", "dict", "index, processors, reserved, pinned, lane_id."),
    ("on_core", "", "bool", "Сейчас код на lane."),
    ("reserve_core", "", "None", "Снова снять ядро lane с остальных потоков."),
    ("json", "method, *args", "object | None", "Вызвать текстовый геттер и разобрать JSON."),
    ("world", "", "object | None", "Снимок мира."),
    ("metrics", "", "object | None", "Метрики сервера."),
    ("ask", "name, data='', callback=None", "list", "Спросить шину и дождаться ответов. Только с worker."),
    ("http_request", "method, url, callback, headers=None, body=None", "Handle", "Ответ приходит на worker."),
    ("set_log_sink", "fn | None", "Handle | None", "Сток лога. Вызывается с потока, который пишет строку."),
    ("relay_filter", "fn", "Handle", "Фильтр ретрансляции. Только быстрые данные, без изменения состояния."),
    ("get_players", "", "list[int]", "Id игроков."),
    ("get_vehicles", "", "list[int]", "Id машин."),
    ("get_player_vehicles", "player_id", "list[int]", "Машины игрока."),
    ("get_player_vehicle", "player_id", "(gid, role)", "Текущая машина. gid -1, если пешком."),
    ("get_vehicles_by_tag", "key, value=None", "list[int]", "Машины с тегом."),
    ("get_vehicles_by_tags", "pairs", "list[int]", "Машины со всеми парами тегов."),
    ("get_vehicle_info", "global_id", "dict | None", "Сводка по машине."),
    ("get_vehicle_transform", "global_id", "dict | None", "Положение и поворот."),
    ("get_vehicle_controls", "global_id", "dict | None", "Органы управления."),
    ("get_player_position", "player_id", "tuple | None", "(x, y, z)."),
    ("get_vehicle_config_hash", "global_id", "int | None", "Хеш конфига."),
    ("get_vehicle_damage_blob", "global_id", "tuple | None", "(bytes, counter)."),
    ("get_vehicle_lock", "global_id, limit=128", "dict | None", "Режим замка и белый список."),
    ("set_vehicle_lock", "global_id, mode, whitelist=()", "int", "Поставить замок."),
    ("get_vehicle_occupants", "global_id", "list[dict]", "Кто сидит."),
    ("sha256_hex", "data", "str", "SHA-256 в hex."),
    ("hmac_sha256_hex", "key, data", "str", "HMAC-SHA256 в hex."),
    ("random_bytes", "n", "bytes", "1..65536 байт."),
    ("emit_client_bytes", "player_id, event, data", "int", "Событие с двоичным телом."),
    ("emit_all_bytes", "event, data", "None", "То же всем."),
    ("send_module", "player_id, channel, data", "int", "Канал модуля."),
    ("resource_name", "", "str | None", "Поле. Имя ресурса, чей код сейчас выполняется."),
    ("resource_dir", "", "str | None", "Поле. Папка ресурса."),
    ("trusted", "", "bool", "Поле. Ресурс есть в server.toml [resources] trusted."),
    ("mojo", "", "module", "Поле. Загрузка библиотеки Mojo из доверенного ресурса."),
]

MPY_EXTRA = [
    ("on", "name, fn", "int", "Слот подписки. Запрос, vehicle, builtin или клиентское."),
    ("off", "slot", "None", "Снять событие. Таймер гасит cancel, не off."),
    ("set_timeout", "ms, fn", "int", "Id таймера. В функцию приходит timer_id."),
    ("set_interval", "ms, fn", "int", "Id таймера."),
    ("after", "ms, fn", "int", "То же, что set_timeout."),
    ("every", "ms, fn", "int", "То же, что set_interval."),
    ("cancel", "timer_id", "None", "Остановить таймер."),
    ("job", "fn, *args, done=None", "None", "fn на закреплённом ядре. done(result, error) на worker."),
    ("core_info", "", "dict", "index, processors, reserved, pinned, lane_id."),
    ("on_core", "", "bool", "Сейчас код на lane."),
    ("reserve_core", "", "None", "Снова снять ядро lane с остальных потоков."),
    ("get_players", "", "list", "Id игроков."),
    ("get_vehicles", "", "list", "Id машин."),
    ("get_player_vehicles", "player_id", "list", "Машины игрока."),
    ("get_player_vehicle", "player_id", "tuple", "(gid, role)."),
    ("get_vehicles_by_tag", "key, value=None", "list", "Машины с тегом."),
    ("get_vehicles_by_tags", "pairs", "list", "Машины со всеми парами тегов."),
    ("resource_name", "", "str | None", "Поле. Имя текущего ресурса."),
    ("resource_dir", "", "str | None", "Поле. Папка ресурса."),
    ("trusted", "", "bool", "Поле. Флаг из server.toml. Дополнительных возможностей не открывает."),
]

MPY_NAMES = {row[0] for row in MPY_EXTRA}


def abi_marks(text: str) -> dict[str, str]:
    marks = {}
    for match in re.finditer(r"\(\*\s*(\w+)\s*\)", text):
        before = text[max(0, match.start() - 4000) : match.start()]
        cut = before.rfind("(*")
        if cut != -1:
            semi = before.find(";", cut)
            before = before[semi + 1 :] if semi != -1 else before[cut:]
        found = re.findall(r"ABI (\d+)\.(\d+)", before)
        marks[match.group(1)] = f"{found[-1][0]}.{found[-1][1]}" if found else ""
    return marks


def newer_than_server(mark: str) -> bool:
    if not mark:
        return False
    major, minor = (int(part) for part in mark.split("."))
    return (major, minor) > SERVER_ABI


def signature(fn: dict) -> str:
    params = [param["name"] for param in fn["params"] if param["kind"] not in ("buf", "buflen")]
    return ", ".join(params)


def returns(fn: dict) -> str:
    if fn["auto"] == "text":
        return "str | None"
    if fn["ret"] == "void":
        return "None"
    if fn["ret"] in ("double", "float"):
        return "float"
    return "int"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", choices=("python", "micropython"), default="python")
    args = parser.parse_args()
    text = HEADER.read_text(encoding="utf-8")
    model = parse_header(text)
    marks = abi_marks(text)
    extra = PYTHON_EXTRA if args.host == "python" else MPY_EXTRA
    wrapped = {row[0] for row in extra}
    lines = [
        "# Функции `node`",
        "",
        "Источник имён — `sdk/node.h`, ABI 2.8. Сервер NodeMP 1.12.0 говорит на ABI 2.7.",
        "Предупреждение `NEWER SDK (2.8 vs 2.7)` в логе значит именно это: хост новее сервера.",
        "Функция из раздела «нет на сервере 1.12.0» при вызове даёт ошибку, сервер от этого не падает.",
        "",
        "Живой список в скрипте ресурса:",
        "",
        "```python",
        "names = sorted(name for name in dir(node) if not name.startswith('_'))",
        "node.log_info(' '.join(names))",
        "```",
        "",
        "`dir(node)` показывает имена, которые объект реально имеет. Описание аргументов каждой функции NodeApi — комментарий над ней в `sdk/node.h`.",
        "",
        "## Методы хоста",
        "",
        "| Имя | Аргументы | Возврат | Смысл |",
        "|---|---|---|---|",
    ]
    for name, params, ret, meaning in extra:
        lines.append(f"| `{name}` | {params or '—'} | {ret} | {meaning} |")
    ready = []
    missing = []
    absent = []
    for fn in model["functions"]:
        mark = marks.get(fn["name"], "")
        row = (fn, mark)
        if args.host == "micropython" and fn["auto"] == "manual" and fn["name"] not in wrapped:
            absent.append(row)
        elif newer_than_server(mark):
            missing.append(row)
        elif fn["auto"] == "manual" and fn["name"] in wrapped:
            continue
        elif fn["auto"] == "manual" and args.host == "python" and fn["name"] in wrapped:
            continue
        else:
            ready.append(row)
    # Manual functions that are also host methods were listed above. Plain and text stay in the NodeApi table.
    ready = [row for row in ready if not (row[0]["auto"] == "manual" and row[0]["name"] in wrapped)]

    def dump(title: str, rows: list, note: str) -> None:
        lines.extend(["", f"## {title}", "", note, "", "| Имя | Аргументы | Возврат | В SDK |", "|---|---|---|---|"])
        for fn, mark in rows:
            shown = (signature(fn) or "—").replace("|", "\\|")
            ret = returns(fn).replace("|", "\\|")
            lines.append(f"| `{fn['name']}` | {shown} | {ret} | {mark or '—'} |")

    dump(
        "NodeApi, которые сервер 1.12.0 отдаёт",
        ready,
        "Строковые геттеры (в заголовке это пара `buf, buflen`) здесь уже без буфера: они возвращают `str` или `None`.",
    )
    dump(
        "Нет на сервере 1.12.0",
        missing,
        "Эти имена объявлены в SDK 2.8. Сервер 1.12.0 их не содержит.",
    )
    if absent:
        dump(
            "Есть в SDK, в этом хосте не обёрнуты",
            absent,
            "В MicroPython этих вызовов нет. Они есть у хоста `type = \"python\"`.",
        )
    lines.append("")
    out = ROOT / "API.md"
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
