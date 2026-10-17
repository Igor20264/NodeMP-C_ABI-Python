# Тест нагрузки. Команда из чата или из консоли сервера:
#   /load start [секунды] [чтений за тик] [задач на ядро за тик]
#   /load stop
#   /load status
#   /load spawn [сколько]
#   /load clear
# serverTick только ставит одну задачу на ядро Python.
# На ядре сначала снимки мира, потом счёт. Пока задача не вернулась,
# следующие тики её не ставят.

import json
import time

DEFAULT_SECONDS = 10
DEFAULT_READS = 1
DEFAULT_JOBS = 2
JOB_ITERS = 200_000
SPAWN_CONFIG = '{"jbm":"pickup"}'
FRAME_MS = 100.0
STALL_MS = 250.0

running = False
run_id = 0
busy = False
deadline = 0.0
reads_per_tick = DEFAULT_READS
jobs_per_tick = DEFAULT_JOBS
ticks = 0
skipped = 0
reads = 0
read_ms = 0.0
read_max = 0.0
worker_ms = 0.0
worker_max = 0.0
jobs_sent = 0
jobs_done = 0
job_errors = 0
jobs_rejected = 0
lane_ms = 0.0
lane_max = 0.0
spawned = []
next_report = 0.0
pending = []


def burn(n):
    total = 0
    for i in range(int(n)):
        total += i
    return total


def lane_work(read_n, passes, iters, player_ids):
    started = time.perf_counter()
    nreads = 0
    for _ in range(int(read_n)):
        node.world()
        nreads += 1
    for player in player_ids:
        node.get_player_name(player)
        nreads += 1
    read_spent = (time.perf_counter() - started) * 1000.0
    burn_started = time.perf_counter()
    total = 0
    for _ in range(int(passes)):
        total += burn(iters)
    burn_spent = (time.perf_counter() - burn_started) * 1000.0
    return (read_spent, burn_spent, nreads, total)


def on_job(result, error, generation):
    global busy, jobs_done, job_errors, reads, read_ms, read_max, lane_ms, lane_max
    if generation != run_id:
        return
    busy = False
    jobs_done += 1
    if error:
        job_errors += 1
        node.log_error(error)
        return
    if not isinstance(result, tuple) or len(result) < 3:
        return
    read_spent = float(result[0])
    burn_spent = float(result[1])
    reads += int(result[2])
    read_ms += read_spent
    lane_ms += burn_spent
    if read_spent > read_max:
        read_max = read_spent
    if burn_spent > lane_max:
        lane_max = burn_spent


def message_text(data):
    if not data:
        return ""
    raw = str(data).strip()
    if raw[:1] not in "{[":
        return raw
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(obj, dict):
        return str(obj.get("text") or "")
    return raw


def picture():
    worker = worker_ms / ticks if ticks else 0.0
    read = read_ms / jobs_done if jobs_done else 0.0
    burn = lane_ms / jobs_done if jobs_done else 0.0
    skip_ratio = skipped / ticks if ticks else 0.0
    return worker, read, burn, read + burn, skip_ratio


def judge(frame_max, over):
    _worker, _read, _burn, core, skip_ratio = picture()
    if over > 0 or frame_max >= STALL_MS or job_errors or jobs_rejected:
        return "плохо", "игра замирает: кадр длиннее 250 мс или были ошибки"
    if frame_max > FRAME_MS:
        return "на пределе", "фриза нет, но кадр длиннее бюджета 100 мс"
    if core > FRAME_MS or skip_ratio > 0.2:
        return "на пределе", "игроки не ждут, ядро не успевает закончить работу за 100 мс"
    return "хорошо", "кадр короче 100 мс, ядро успевает, замираний нет"


def report(prefix):
    worker, read, burn, core, _skip = picture()
    title, _why = judge(worker_max, 0)
    node.log_info(
        f"loadtest: {prefix} | игроки {worker:.0f} мс из {FRAME_MS:.0f} | "
        f"чтение {read:.0f} мс | счёт {burn:.0f} мс | ядро {core:.0f} мс | "
        f"пропущено {skipped} из {ticks} | {title}"
    )


def handler_stats():
    try:
        text = node.get_handler_stats_json(1)
    except Exception:
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def finish(reason):
    global running
    if not running and reason != "stop":
        return
    running = False
    stats = handler_stats()
    frame_max = worker_max
    over = 0
    if stats:
        try:
            frame_max = float(stats.get("max") or worker_max)
            over = int(stats.get("over") or 0)
        except (TypeError, ValueError):
            frame_max = worker_max
    title, why = judge(frame_max, over)
    _worker, read, burn, core, _skip = picture()
    node.log_info(f"loadtest: вердикт — {title}. {why}")
    node.log_info(
        f"loadtest: игроки — худший кадр {frame_max:.0f} мс, бюджет {FRAME_MS:.0f} мс, "
        f"замирание с {STALL_MS:.0f} мс, замираний {over}"
    )
    node.log_info(
        f"loadtest: ядро — чтение {read:.0f} мс + счёт {burn:.0f} мс = {core:.0f} мс, "
        f"пропущено {skipped} тиков из {ticks}, ошибок {job_errors}, "
        f"отказов {jobs_rejected}, машин {len(spawned)}"
    )


def start(seconds, reads_n, jobs_n):
    global running, run_id, busy, deadline, reads_per_tick, jobs_per_tick
    global ticks, skipped, reads, read_ms, read_max, worker_ms, worker_max
    global jobs_sent, jobs_done, job_errors, jobs_rejected, lane_ms, lane_max, next_report
    run_id += 1
    busy = False
    reads_per_tick = max(0, int(reads_n))
    jobs_per_tick = max(0, int(jobs_n))
    ticks = 0
    skipped = 0
    reads = 0
    read_ms = 0.0
    read_max = 0.0
    worker_ms = 0.0
    worker_max = 0.0
    jobs_sent = 0
    jobs_done = 0
    job_errors = 0
    jobs_rejected = 0
    lane_ms = 0.0
    lane_max = 0.0
    running = True
    deadline = time.monotonic() + max(1, int(seconds))
    next_report = time.monotonic() + 1.0
    node.log_info(
        f"loadtest: started {int(seconds)}s, {reads_per_tick} reads/job, "
        f"{jobs_per_tick} burns/job, queued from serverTick onto the python core"
    )


def spawn_cars(count):
    made = 0
    for _ in range(max(0, int(count))):
        gid = node.spawn_vehicle(SPAWN_CONFIG, "loadtest")
        if not isinstance(gid, int) or gid < 0:
            node.log_error(f"loadtest: spawn failed ({gid})")
            break
        spawned.append(gid)
        made += 1
    node.log_info(f"loadtest: spawned {made}, live {len(spawned)}")


def remove_cars():
    gone = 0
    while spawned:
        gid = spawned.pop()
        if node.delete_vehicle(gid) == 0:
            gone += 1
    if gone:
        node.log_info(f"loadtest: removed {gone} cars")


def handle(line):
    parts = line.split()
    if not parts or parts[0].lower() not in {"/load", "load"}:
        return False
    cmd = parts[1].lower() if len(parts) > 1 else "help"
    if cmd == "start":
        seconds = parts[2] if len(parts) > 2 else DEFAULT_SECONDS
        reads_n = parts[3] if len(parts) > 3 else DEFAULT_READS
        jobs_n = parts[4] if len(parts) > 4 else DEFAULT_JOBS
        start(seconds, reads_n, jobs_n)
    elif cmd == "stop":
        finish("stop")
    elif cmd == "status":
        report("running" if running else "idle")
    elif cmd == "spawn":
        spawn_cars(parts[2] if len(parts) > 2 else 1)
    elif cmd == "clear":
        remove_cars()
    else:
        node.log_info(
            "loadtest: /load start [seconds] [reads] [jobs] | stop | status | spawn [n] | clear"
        )
    return True


def on_chat(player_id, global_id, data):
    del player_id, global_id
    text = message_text(data)
    if text.lower().startswith("/load"):
        handle(text)
    return None


def on_wire(player_id, data):
    del player_id
    text = message_text(data)
    if not text.lower().startswith("/load"):
        text = "/load " + text
    handle(text)


def on_sink(tag, level, msg):
    del level
    if tag == "Console" and msg and str(msg).lstrip().lower().startswith("/load"):
        pending.append(str(msg).strip())
    return False


def on_tick(player_id):
    global ticks, skipped, worker_ms, worker_max, jobs_sent, jobs_rejected, busy, next_report
    del player_id
    while pending:
        handle(pending.pop(0))
    if not running:
        return
    if time.monotonic() >= deadline:
        finish("done")
        return
    started = time.perf_counter()
    if busy:
        skipped += 1
    elif reads_per_tick or jobs_per_tick:
        players = list(node.get_players())
        try:
            generation = run_id
            node.job(
                lane_work,
                reads_per_tick,
                jobs_per_tick,
                JOB_ITERS,
                players,
                done=lambda result, error, generation=generation: on_job(result, error, generation),
            )
            jobs_sent += 1
            busy = True
        except RuntimeError:
            jobs_rejected += 1
    spent = (time.perf_counter() - started) * 1000.0
    worker_ms += spent
    if spent > worker_max:
        worker_max = spent
    ticks += 1
    now = time.monotonic()
    if now >= next_report:
        next_report = now + 1.0
        report("tick")


node.on_request("chatMessageRequest", on_chat)
node.on("loadtest", on_wire)
node.on("serverTick", on_tick)
node.set_log_sink(on_sink)
node.log_info("loadtest idle: /load start [seconds] [reads] [jobs]  (chat or console)")


def unload():
    global running
    running = False
    node.set_log_sink(None)
    remove_cars()
    node.log_info("loadtest unloaded")
