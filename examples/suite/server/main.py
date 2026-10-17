# Объёмный тест. Чат: /suite start | stop | status
# Спавна нет. Счёт в фоне уходит на ядро Python.

import time

HOST = "Python"
HAS_BACKGROUND = True
STEP_SECONDS = 10
FRAME_MS = 100
STALL_MS = 250
JOB_ITERS = 200000
BURST_N = 200
BURST_ITERS = 1000

STEPS = [
    {"name": "пустой кадр", "kind": "idle", "n": 0},
    {"name": "чтение 10", "kind": "read", "n": 10},
    {"name": "чтение 100", "kind": "read", "n": 100},
    {"name": "чтение 500", "kind": "read", "n": 500},
    {"name": "счёт в кадре 10", "kind": "inline", "n": 10},
    {"name": "счёт в кадре 100", "kind": "inline", "n": 100},
    {"name": "счёт в кадре 500", "kind": "inline", "n": 500},
    {"name": "счёт в фоне 10", "kind": "back", "n": 10},
    {"name": "счёт в фоне 100", "kind": "back", "n": 100},
    {"name": "счёт в фоне 500", "kind": "back", "n": 500},
    {"name": "пачка", "kind": "burst", "n": BURST_N},
    {"name": "перекрытие", "kind": "overlap", "n": 3000},
    {"name": "ожидание", "kind": "wait", "n": 40},
]


def burnJob(passes, iters):
    started = clock()
    total = 0
    for _ in range(passes):
        part = 0
        for i in range(iters):
            part += i
        total += part
    return (clock() - started) * 1000


def now():
    return node.get_server_uptime()


def clock():
    return time.perf_counter()


def say(text):
    node.log_info(text)


def readOnce():
    node.get_world_snapshot_json()
    for player_id in node.get_players():
        node.get_player_name(player_id)


def every(ms, fn):
    return node.every(ms, fn)


def cancel(timer):
    if timer is not None:
        node.cancel(timer)


def startJob(passes, iters, done):
    def finish(result, error):
        if error:
            done(0, error)
        else:
            done(result or 0, None)

    node.job(burnJob, passes, iters, done=finish)


def startWait(n, done):
    def one(timer_id=None):
        done()

    for _ in range(n):
        node.after(50, one)


running = False
runId = 0
serial = 0
busy = False
timerId = None
stepIndex = 1
phase = None
rows = []
startedAt = 0
deadline = 0
nextBeat = 0
overlapArmed = False
waitArmed = False
ticks = 0
skipped = 0
reads = 0
readMs = 0
readMax = 0
workerMs = 0
workerMax = 0
jobsSent = 0
jobsDone = 0
jobErrors = 0
jobsRejected = 0
burnMs = 0
burnMax = 0
stallCount = 0
waitsDone = 0


def resetPhase():
    global busy, overlapArmed, waitArmed, ticks, skipped, reads, readMs, readMax
    global workerMs, workerMax, jobsSent, jobsDone, jobErrors, jobsRejected
    global burnMs, burnMax, stallCount, waitsDone
    busy = False
    overlapArmed = False
    waitArmed = False
    ticks = 0
    skipped = 0
    reads = 0
    readMs = 0
    readMax = 0
    workerMs = 0
    workerMax = 0
    jobsSent = 0
    jobsDone = 0
    jobErrors = 0
    jobsRejected = 0
    burnMs = 0
    burnMax = 0
    stallCount = 0
    waitsDone = 0


def dueTicks():
    elapsed = now() - startedAt
    if elapsed < 0:
        elapsed = 0
    due = int(elapsed * 10 + 0.5)
    if due < ticks:
        due = ticks
    cap = STEP_SECONDS * 10
    if due > cap:
        due = cap
    return due


def counts():
    kind = phase["kind"]
    due = STEP_SECONDS * 10
    if kind == "back":
        return jobsDone, due
    if kind == "burst":
        return jobsDone, ticks * BURST_N
    if kind == "overlap":
        return jobsDone, 1
    if kind == "wait":
        return waitsDone, phase["n"]
    return ticks, due


def picture():
    read = (readMs / ticks) if ticks else 0
    burn = (burnMs / jobsDone) if jobsDone else 0
    doneN, planN = counts()
    return workerMax, read, burn, doneN, planN


def judge(frame):
    due = dueTicks()
    missed = due - ticks
    if missed < 0:
        missed = 0
    missRatio = (missed / due) if due else 0
    if stallCount > 0 or frame >= STALL_MS or jobErrors > 0 or jobsRejected > 0:
        return "плохо"
    if frame > FRAME_MS or missRatio > 0.2:
        return "на пределе"
    burn = (burnMs / jobsDone) if jobsDone else 0
    if burn > FRAME_MS or (ticks > 0 and skipped / ticks > 0.2):
        return "на пределе"
    return "хорошо"


def lineFor(prefix):
    frame, read, burn, doneN, planN = picture()
    text = "%s | %s | кадр %.0f мс | чтение %.0f мс | счёт %.0f мс | сделано %d из %d | замираний %d | %s" % (
        prefix, phase["name"], frame, read, burn, doneN, planN, stallCount, judge(frame)
    )
    if phase["kind"] == "back" and not HAS_BACKGROUND:
        text += " | фона нет"
    return text


def summary():
    say("сводка " + HOST)
    for text in rows:
        say(text)


def beginStep():
    global phase, serial, startedAt, deadline, nextBeat
    phase = STEPS[stepIndex - 1]
    serial += 1
    resetPhase()
    startedAt = now()
    deadline = startedAt + STEP_SECONDS
    nextBeat = startedAt + 1
    say("старт | " + phase["name"])


def finishStep():
    global running, timerId, stepIndex
    text = lineFor("итог")
    rows.append(text)
    say(text)
    stepIndex += 1
    if stepIndex > len(STEPS):
        running = False
        if timerId is not None:
            cancel(timerId)
            timerId = None
        summary()
        return
    beginStep()


def onTick(timer_id=None):
    global busy, jobsSent, jobsDone, jobErrors, jobsRejected, skipped
    global readMs, readMax, reads, burnMs, burnMax
    global workerMs, workerMax, stallCount, ticks, nextBeat
    global overlapArmed, waitArmed, waitsDone
    if not running or phase is None:
        return
    if now() >= deadline:
        finishStep()
        return
    started = clock()
    kind = phase["kind"]
    n = phase["n"]
    token = serial
    if kind == "read":
        readStarted = clock()
        for _ in range(n):
            readOnce()
        spentRead = (clock() - readStarted) * 1000
        readMs += spentRead
        if spentRead > readMax:
            readMax = spentRead
        reads += n
    elif kind == "inline":
        ms = burnJob(n, JOB_ITERS)
        burnMs += ms
        if ms > burnMax:
            burnMax = ms
        jobsSent += 1
        jobsDone += 1
    elif kind == "back":
        if busy:
            skipped += 1
        else:
            busy = True

            def finish(ms, jobErr, token=token):
                global busy, jobsDone, jobErrors, burnMs, burnMax
                if token != serial:
                    return
                busy = False
                jobsDone += 1
                if jobErr:
                    jobErrors += 1
                    say(str(jobErr))
                    return
                burnMs += ms or 0
                if (ms or 0) > burnMax:
                    burnMax = ms or 0

            try:
                startJob(n, JOB_ITERS, finish)
                jobsSent += 1
            except Exception as exc:
                busy = False
                jobsRejected += 1
                say(str(exc))
    elif kind == "burst":
        for _ in range(BURST_N):
            def finish(ms, jobErr, token=token):
                global jobsDone, jobErrors, burnMs, burnMax
                if token != serial:
                    return
                jobsDone += 1
                if jobErr:
                    jobErrors += 1
                    return
                burnMs += ms or 0
                if (ms or 0) > burnMax:
                    burnMax = ms or 0

            try:
                startJob(1, BURST_ITERS, finish)
                jobsSent += 1
            except Exception as exc:
                jobsRejected += 1
                say(str(exc))
                break
    elif kind == "overlap":
        if not overlapArmed:
            overlapArmed = True

            def finish(ms, jobErr, token=token):
                global jobsDone, jobErrors, burnMs, burnMax
                if token != serial:
                    return
                jobsDone += 1
                if jobErr:
                    jobErrors += 1
                    say(str(jobErr))
                    return
                burnMs += ms or 0
                if (ms or 0) > burnMax:
                    burnMax = ms or 0

            try:
                startJob(n, JOB_ITERS, finish)
                jobsSent += 1
            except Exception as exc:
                jobsRejected += 1
                say(str(exc))
    elif kind == "wait":
        if not waitArmed:
            waitArmed = True

            def done(token=token):
                global waitsDone
                if token != serial:
                    return
                waitsDone += 1

            startWait(n, done)
    spent = (clock() - started) * 1000
    workerMs += spent
    if spent > workerMax:
        workerMax = spent
    if spent >= STALL_MS:
        stallCount += 1
    ticks += 1
    if now() >= nextBeat:
        nextBeat = now() + 1
        say(lineFor("тик"))


def start():
    global running, runId, serial, rows, stepIndex, timerId
    if timerId is not None:
        cancel(timerId)
        timerId = None
    runId += 1
    serial += 1
    running = True
    rows = []
    stepIndex = 1
    beginStep()
    timerId = every(100, onTick)
    say(HOST + " | /suite start | stop | status")


def stop():
    global running, serial, timerId
    if not running:
        say("idle | /suite start")
        return
    running = False
    serial += 1
    if timerId is not None:
        cancel(timerId)
        timerId = None
    say("стоп | " + (phase["name"] if phase else ""))
    summary()


def status():
    if running and phase is not None:
        say(lineFor("тик"))
    else:
        say("idle | /suite start")


def words(line):
    return str(line).split()


def handle(line):
    parts = words(line)
    if not parts:
        return False
    head = parts[0].lower()
    if head != "/suite" and head != "suite":
        return False
    cmd = parts[1].lower() if len(parts) > 1 else "help"
    if cmd == "start":
        start()
    elif cmd == "stop":
        stop()
    elif cmd == "status":
        status()
    else:
        say("/suite start | stop | status")
    return True


def chatText(data):
    text = data if isinstance(data, str) else ""
    if not text.startswith("{"):
        return text
    at = text.find('"text"')
    if at < 0:
        return text
    colon = text.find(":", at + 6)
    if colon < 0:
        return text
    quote = text.find('"', colon + 1)
    if quote < 0:
        return text
    end = text.find('"', quote + 1)
    if end < 0:
        return text
    return text[quote + 1:end]


def onChat(player_id, global_id, data):
    handle(chatText(data))
    return None


def unload():
    global running, timerId
    running = False
    if timerId is not None:
        cancel(timerId)
        timerId = None


node.on_request("chatMessageRequest", onChat)
say(HOST + " idle | /suite start | stop | status")
