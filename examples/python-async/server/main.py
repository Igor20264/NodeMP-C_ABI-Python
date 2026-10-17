# Событие можно отпустить сразу. Запрос — нет: клиент ждёт вердикт,
# пока функция не вернулась. async def здесь не запускается. Для запроса
# корутина ещё и читается как разрешение.

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


def deliver(result, error):
    if error:
        node.log_error(error)
        return
    node.log_info(f"python-async: welcome for {result['name']} ready on core {result['core']}")
    node.emit_client(result["player_id"], "greeting", f"Welcome, {result['name']}")


def on_join(player_id):
    name = node.get_player_name(player_id) or "?"
    node.log_info(f"python-async: queued welcome for {name}")
    node.job(prepare, player_id, name, done=deliver)


def census(ids):
    names = []
    for player_id in ids:
        names.append(node.get_player_name(player_id) or "?")
    return {"names": names, "core": node.core_info()["index"]}


def show_census(result, error):
    if error:
        node.log_error(error)
        return
    listed = ", ".join(result["names"]) if result["names"] else "nobody"
    node.log_info(
        f"python-async: {len(result['names'])} online ({listed}) counted on core {result['core']}"
    )


def on_census(timer_id):
    del timer_id
    node.job(census, list(node.get_players()), done=show_census)


node.on("playerConnectRequest", on_connect)
node.on("playerJoined", on_join)
clock = node.every(60000, on_census)

for player_id in node.get_players():
    on_join(player_id)

node.log_info("python-async loaded")


def unload():
    node.cancel(clock)
    node.log_info("python-async unloaded")
