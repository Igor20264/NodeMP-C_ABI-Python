# Запрос отвечает до выхода из функции. async def у запроса нельзя останавливать
# на await: клиент ждёт вердикт сразу. Событие можно писать как async def.
# await node.job ждёт закреплённое ядро и не держит worker.

import time

BLOCKED = {"Player9"}


def on_connect(player_id, global_id, name):
    del player_id, global_id
    if name in BLOCKED:
        return "You are not allowed on this server"
    return None


def prepare(player_id, name):
    time.sleep(0.2)
    mark = 0
    for n in range(200000):
        mark += n
    return {
        "player_id": player_id,
        "name": name,
        "core": node.core_info()["index"],
        "mark": mark,
    }


async def on_join(player_id):
    name = node.get_player_name(player_id) or "?"
    node.log_info(f"python-async: queued welcome for {name}")
    result = await node.job(prepare, player_id, name)
    node.log_info(f"python-async: welcome for {result['name']} ready on core {result['core']}")
    node.emit_client(result["player_id"], "greeting", f"Welcome, {result['name']}")


def census(ids):
    names = []
    for player_id in ids:
        names.append(node.get_player_name(player_id) or "?")
    return {"names": names, "core": node.core_info()["index"]}


async def on_census(timer_id):
    del timer_id
    result = await node.job(census, list(node.get_players()))
    listed = ", ".join(result["names"]) if result["names"] else "nobody"
    node.log_info(
        f"python-async: {len(result['names'])} online ({listed}) counted on core {result['core']}"
    )


node.on("playerConnectRequest", on_connect)
node.on("playerJoined", on_join)
clock = node.every(60000, on_census)

for player_id in node.get_players():
    node.spawn(on_join(player_id))

node.log_info("python-async loaded")


def unload():
    node.cancel(clock)
    node.log_info("python-async unloaded")
