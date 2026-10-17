/* NodeMP language host for Python 3.14 and 3.15.
 *
 * The server owns one worker thread and delivers every callback there.
 * This module embeds CPython on that arrangement and adds one more thread,
 * the core lane. The lane is pinned to a single logical processor. While it
 * is alive, other threads in the process lose that processor in their
 * affinity mask, so background Python and Mojo work stays off the worker.
 *
 * Mojo is loaded as a shared library. Native calls are queued onto the lane;
 * ctypes drops the GIL for the call, which is what lets that core run Mojo
 * without stalling a callback that needs the interpreter.
 */

#define PY_SSIZE_T_CLEAN

#if defined(__linux__)
#define _GNU_SOURCE
#endif

#if defined(_WIN32)
#define _CRT_SECURE_NO_WARNINGS
#define WIN32_LEAN_AND_MEAN
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif
#include <windows.h>
#include <tlhelp32.h>
#include <process.h>
#endif

#include <Python.h>
#include "node.h"

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#if !defined(_WIN32)
#include <dirent.h>
#include <dlfcn.h>
#include <pthread.h>
#include <sched.h>
#include <unistd.h>
#if defined(__APPLE__)
#include <mach/thread_act.h>
#endif
#if defined(__linux__)
#include <sys/syscall.h>
#endif
#endif

#define JOB_PY 0
#define JOB_SYNC 1
#define JOB_CAP 4096
#define MOJO_MAX 32

#if defined(_WIN32)
#define STOP_GET() InterlockedCompareExchange(&g_stop, 0, 0)
#define STOP_SET() InterlockedExchange(&g_stop, 1)
static long g_stop;
#else
#define STOP_GET() __atomic_load_n(&g_stop, __ATOMIC_ACQUIRE)
#define STOP_SET() __atomic_store_n(&g_stop, 1, __ATOMIC_RELEASE)
static long g_stop;
#endif

typedef struct Job {
    int kind;
    PyObject* fn;
    PyObject* done;
    void (*cfn)(void);
    int finished;
    struct Job* next;
} Job;

typedef struct DoneBox {
    PyObject* done;
    PyObject* args;
} DoneBox;

static const NodeApi* g_api;
static PyObject* g_runtime;
static PyThreadState* g_saved;
static int g_live;

static Job* g_head;
static Job* g_tail;
static int g_count;
static int g_ready_flag;
static int g_lane_up;
static int g_thread_started;
static int g_core = -1;
static int g_ncpu;
static int g_reserve;
static unsigned long long g_reserve_log_at;

#if defined(_WIN32)
static CRITICAL_SECTION g_mu;
static CONDITION_VARIABLE g_cv;
static CRITICAL_SECTION g_reserve_mu;
static HANDLE g_thread;
static DWORD g_lane_tid;
static HMODULE g_module;
#else
static pthread_mutex_t g_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t g_cv = PTHREAD_COND_INITIALIZER;
static pthread_mutex_t g_reserve_mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_t g_thread;
static pthread_t g_lane_thread;
static int g_lane_valid;
#if defined(__linux__)
static long g_lane_os_id;
#endif
#endif

static void* g_mojo[MOJO_MAX];

static void log_line(const char* msg) {
    if (!g_api || !msg) return;
    if (g_api->log_custom) g_api->log_custom("Py", 0x3DDC97u, msg);
    else if (g_api->log_info) g_api->log_info(msg);
}

static void log_err(const char* msg) {
    if (g_api && g_api->log_error && msg) g_api->log_error(msg);
}

static void q_lock(void) {
#if defined(_WIN32)
    EnterCriticalSection(&g_mu);
#else
    pthread_mutex_lock(&g_mu);
#endif
}

static void q_unlock(void) {
#if defined(_WIN32)
    LeaveCriticalSection(&g_mu);
#else
    pthread_mutex_unlock(&g_mu);
#endif
}

static void q_wait(void) {
#if defined(_WIN32)
    SleepConditionVariableCS(&g_cv, &g_mu, INFINITE);
#else
    pthread_cond_wait(&g_cv, &g_mu);
#endif
}

static void q_broadcast(void) {
#if defined(_WIN32)
    WakeAllConditionVariable(&g_cv);
#else
    pthread_cond_broadcast(&g_cv);
#endif
}

static int on_lane(void) {
#if defined(_WIN32)
    return g_lane_tid != 0 && GetCurrentThreadId() == g_lane_tid;
#else
    return g_lane_valid && pthread_equal(pthread_self(), g_lane_thread);
#endif
}

static unsigned long long tick_ms(void) {
#if defined(_WIN32)
    return GetTickCount64();
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (unsigned long long)ts.tv_sec * 1000ull + (unsigned long long)ts.tv_nsec / 1000000ull;
#endif
}

static void log_reserve(int moved) {
    unsigned long long now;
    char buf[160];
    if (moved <= 0) return;
    now = tick_ms();
    if (g_reserve_log_at && now - g_reserve_log_at < 10000ull) return;
    g_reserve_log_at = now;
    snprintf(buf, sizeof buf, "moved %d thread(s) off core %d so the python lane keeps it", moved, g_core);
    log_line(buf);
}

static void pin_this_thread(void) {
#if defined(_WIN32)
    GROUP_AFFINITY affinity;
    PROCESSOR_NUMBER ideal;
    if (g_core < 0) return;
    ZeroMemory(&affinity, sizeof affinity);
    affinity.Group = (WORD)(g_core / 64);
    affinity.Mask = ((DWORD_PTR)1) << (g_core % 64);
    if (!SetThreadGroupAffinity(GetCurrentThread(), &affinity, NULL)) {
        log_err("SetThreadGroupAffinity failed; the python lane is not pinned");
        g_core = -1;
        g_reserve = 0;
        return;
    }
    ZeroMemory(&ideal, sizeof ideal);
    ideal.Group = affinity.Group;
    ideal.Number = (BYTE)(g_core % 64);
    SetThreadIdealProcessorEx(GetCurrentThread(), &ideal, NULL);
#elif defined(__APPLE__)
    thread_affinity_policy_data_t policy;
    if (g_core < 0) return;
    policy.affinity_tag = g_core + 1;
    thread_policy_set(pthread_mach_thread_np(pthread_self()), THREAD_AFFINITY_POLICY,
        (thread_policy_t)&policy, THREAD_AFFINITY_POLICY_COUNT);
    g_reserve = 0;
    log_line("macOS tags the python lane with an affinity hint; other threads are not moved");
#else
    cpu_set_t set;
    if (g_core < 0) return;
    CPU_ZERO(&set);
    CPU_SET(g_core, &set);
    if (sched_setaffinity(0, sizeof set, &set) != 0) {
        log_err("sched_setaffinity failed; the python lane is not pinned");
        g_core = -1;
        g_reserve = 0;
    }
#endif
}

static void name_this_thread(void) {
#if defined(_WIN32)
    typedef HRESULT(WINAPI * SetThreadDescriptionFn)(HANDLE, PCWSTR);
    HMODULE kernel = GetModuleHandleW(L"kernel32.dll");
    SetThreadDescriptionFn fn = kernel ? (SetThreadDescriptionFn)GetProcAddress(kernel, "SetThreadDescription") : NULL;
    if (fn) fn(GetCurrentThread(), L"nodemp-python-core");
#elif defined(__APPLE__)
    pthread_setname_np("nodemp-py-core");
#else
    pthread_setname_np(pthread_self(), "nodemp-py-core");
#endif
}

static void core_reserve(void) {
#if defined(_WIN32)
    HANDLE snap;
    THREADENTRY32 entry;
    int moved = 0;
    DWORD pid;
    DWORD_PTR bit;
    WORD group;
    if (!g_reserve || g_core < 0 || g_lane_tid == 0) return;
    EnterCriticalSection(&g_reserve_mu);
    bit = ((DWORD_PTR)1) << (g_core % 64);
    group = (WORD)(g_core / 64);
    pid = GetCurrentProcessId();
    snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
    if (snap == INVALID_HANDLE_VALUE) {
        LeaveCriticalSection(&g_reserve_mu);
        return;
    }
    entry.dwSize = sizeof entry;
    if (Thread32First(snap, &entry)) {
        do {
            HANDLE thread;
            GROUP_AFFINITY affinity;
            if (entry.th32OwnerProcessID != pid || entry.th32ThreadID == g_lane_tid) continue;
            thread = OpenThread(THREAD_SET_INFORMATION | THREAD_QUERY_INFORMATION, FALSE, entry.th32ThreadID);
            if (!thread) continue;
            if (GetThreadGroupAffinity(thread, &affinity) && affinity.Group == group &&
                (affinity.Mask & bit) && affinity.Mask != bit) {
                affinity.Mask &= ~bit;
                if (SetThreadGroupAffinity(thread, &affinity, NULL)) moved++;
            }
            CloseHandle(thread);
        } while (Thread32Next(snap, &entry));
    }
    CloseHandle(snap);
    log_reserve(moved);
    LeaveCriticalSection(&g_reserve_mu);
#elif defined(__linux__)
    DIR* dir;
    struct dirent* ent;
    int moved = 0;
    if (!g_reserve || g_core < 0 || g_lane_os_id == 0) return;
    pthread_mutex_lock(&g_reserve_mu);
    dir = opendir("/proc/self/task");
    if (!dir) {
        pthread_mutex_unlock(&g_reserve_mu);
        return;
    }
    while ((ent = readdir(dir)) != NULL) {
        char* end = NULL;
        long tid = strtol(ent->d_name, &end, 10);
        cpu_set_t set;
        if (end == ent->d_name || tid <= 0 || tid == g_lane_os_id) continue;
        if (sched_getaffinity((pid_t)tid, sizeof set, &set) != 0) continue;
        if (!CPU_ISSET(g_core, &set) || CPU_COUNT(&set) <= 1) continue;
        CPU_CLR(g_core, &set);
        if (sched_setaffinity((pid_t)tid, sizeof set, &set) == 0) moved++;
    }
    closedir(dir);
    log_reserve(moved);
    pthread_mutex_unlock(&g_reserve_mu);
#else
    (void)0;
#endif
}

static void reserve_timer(int timer_id, void* user) {
    (void)timer_id;
    (void)user;
    core_reserve();
}

static int enqueue_locked(Job* job) {
    if (!g_lane_up || STOP_GET() || g_count >= JOB_CAP) return 0;
    job->next = NULL;
    if (g_tail) g_tail->next = job;
    else g_head = job;
    g_tail = job;
    g_count++;
    q_broadcast();
    return 1;
}

static void log_unicode(PyObject* text) {
    const char* msg;
    if (!text) return;
    msg = PyUnicode_AsUTF8(text);
    if (!msg) {
        PyErr_Clear();
        return;
    }
    log_err(msg);
}

static PyObject* format_error(void) {
    PyObject *ty = NULL, *val = NULL, *tb = NULL;
    PyObject *mod = NULL, *lines = NULL, *empty = NULL, *text = NULL;
    PyErr_Fetch(&ty, &val, &tb);
    PyErr_NormalizeException(&ty, &val, &tb);
    mod = PyImport_ImportModule("traceback");
    if (mod) {
        lines = PyObject_CallMethod(mod, "format_exception", "OOO", ty ? ty : Py_None, val ? val : Py_None,
            tb ? tb : Py_None);
        if (lines) {
            empty = PyUnicode_FromString("");
            if (empty) text = PyUnicode_Join(empty, lines);
        }
    }
    PyErr_Clear();
    if (!text && val) text = PyObject_Str(val);
    PyErr_Clear();
    if (!text) text = PyUnicode_FromString("python exception");
    Py_XDECREF(empty);
    Py_XDECREF(lines);
    Py_XDECREF(mod);
    Py_XDECREF(ty);
    Py_XDECREF(val);
    Py_XDECREF(tb);
    return text;
}

static void log_current_error(const char* prefix) {
    PyObject* text;
    const char* msg;
    char buf[1024];
    if (!PyErr_Occurred()) {
        log_err(prefix);
        return;
    }
    text = format_error();
    msg = text ? PyUnicode_AsUTF8(text) : NULL;
    snprintf(buf, sizeof buf, "%s: %.800s", prefix, msg ? msg : "unknown");
    log_err(buf);
    Py_XDECREF(text);
    PyErr_Clear();
}

static void deliver_done(int timer_id, void* user) {
    DoneBox* box = (DoneBox*)user;
    PyGILState_STATE gs;
    PyObject* ret;
    (void)timer_id;
    gs = PyGILState_Ensure();
    ret = PyObject_CallObject(box->done, box->args);
    if (!ret) log_current_error("python job callback");
    else Py_DECREF(ret);
    Py_DECREF(box->done);
    Py_DECREF(box->args);
    PyGILState_Release(gs);
    free(box);
}

static void discard_py(Job* job) {
    PyGILState_STATE gs = PyGILState_Ensure();
    Py_XDECREF(job->fn);
    Py_XDECREF(job->done);
    PyGILState_Release(gs);
    free(job);
}

static void run_py(Job* job) {
    PyGILState_STATE gs = PyGILState_Ensure();
    PyObject* result = PyObject_CallObject(job->fn, NULL);
    PyObject* error = NULL;
    DoneBox* box = NULL;
    int scheduled = 0;
    if (!result) error = format_error();
    if (job->done && !STOP_GET()) {
        box = (DoneBox*)calloc(1, sizeof(DoneBox));
        if (box) {
            box->done = job->done;
            Py_INCREF(job->done);
            box->args = Py_BuildValue("OO", result ? result : Py_None, error ? error : Py_None);
            if (box->args) {
                PyGILState_Release(gs);
                if (g_api->set_timeout(0, deliver_done, box) >= 0) scheduled = 1;
                gs = PyGILState_Ensure();
            }
        }
    }
    if (!scheduled) {
        if (error) log_unicode(error);
        if (box) {
            Py_XDECREF(box->done);
            Py_XDECREF(box->args);
            free(box);
        }
    }
    Py_XDECREF(result);
    Py_XDECREF(error);
    Py_DECREF(job->fn);
    Py_XDECREF(job->done);
    PyGILState_Release(gs);
    free(job);
}

static void lane_body(void) {
    pin_this_thread();
    name_this_thread();
#if defined(_WIN32)
    g_lane_tid = GetCurrentThreadId();
#elif defined(__linux__)
    g_lane_thread = pthread_self();
    g_lane_valid = 1;
    g_lane_os_id = (long)syscall(SYS_gettid);
#else
    g_lane_thread = pthread_self();
    g_lane_valid = 1;
#endif
    core_reserve();
    q_lock();
    g_ready_flag = 1;
    q_broadcast();
    for (;;) {
        Job* job;
        int stopping;
        while (!STOP_GET() && g_head == NULL) q_wait();
        if (g_head == NULL) {
            q_unlock();
            return;
        }
        job = g_head;
        g_head = job->next;
        if (g_head == NULL) g_tail = NULL;
        g_count--;
        stopping = STOP_GET() ? 1 : 0;
        q_unlock();
        if (job->kind == JOB_SYNC) {
            job->cfn();
            q_lock();
            job->finished = 1;
            q_broadcast();
            continue;
        }
        if (stopping) discard_py(job);
        else run_py(job);
        q_lock();
    }
}

#if defined(_WIN32)
static unsigned __stdcall lane_main(void* arg) {
    (void)arg;
    lane_body();
    return 0;
}
#else
static void* lane_main(void* arg) {
    (void)arg;
    lane_body();
    return NULL;
}
#endif

static int processor_count(void) {
#if defined(_WIN32)
    DWORD count = GetActiveProcessorCount(ALL_PROCESSOR_GROUPS);
    return count > 0 ? (int)count : 1;
#else
    long count = sysconf(_SC_NPROCESSORS_ONLN);
    return count > 0 ? (int)count : 1;
#endif
}

static int choose_core(void) {
    const char* env = getenv("NODEMP_PYTHON_CORE");
    char* end = NULL;
    long value;
    char buf[160];
    if (g_ncpu < 1) g_ncpu = 1;
    if (env && (strcmp(env, "off") == 0 || strcmp(env, "any") == 0)) return -1;
    if (!env || !env[0] || strcmp(env, "last") == 0) return g_ncpu <= 1 ? -1 : g_ncpu - 1;
    value = strtol(env, &end, 10);
    if (end == env || *end != '\0' || value < 0 || value >= g_ncpu) {
        snprintf(buf, sizeof buf, "NODEMP_PYTHON_CORE=%s is not a core on this machine; using the last core", env);
        log_line(buf);
        return g_ncpu <= 1 ? -1 : g_ncpu - 1;
    }
    return (int)value;
}

static int reserve_enabled(void) {
    const char* env = getenv("NODEMP_PYTHON_RESERVE");
    if (env && strcmp(env, "0") == 0) return 0;
    return 1;
}

static int core_start(void) {
#if defined(_WIN32)
    InitializeCriticalSection(&g_mu);
    InitializeConditionVariable(&g_cv);
    InitializeCriticalSection(&g_reserve_mu);
#endif
    g_ncpu = processor_count();
    g_core = choose_core();
    g_reserve = reserve_enabled();
    if (g_core < 0) g_reserve = 0;
#if defined(_WIN32)
    g_thread = (HANDLE)_beginthreadex(NULL, 0, lane_main, NULL, 0, NULL);
    if (!g_thread) return -1;
#else
    if (pthread_create(&g_thread, NULL, lane_main, NULL) != 0) return -1;
#endif
    g_thread_started = 1;
    q_lock();
    while (!g_ready_flag) q_wait();
    q_unlock();
    g_lane_up = 1;
    return 0;
}

static void core_stop(void) {
    if (!g_thread_started) return;
    STOP_SET();
    q_lock();
    q_broadcast();
    q_unlock();
#if defined(_WIN32)
    WaitForSingleObject(g_thread, INFINITE);
    CloseHandle(g_thread);
    g_thread = NULL;
#else
    pthread_join(g_thread, NULL);
#endif
    g_thread_started = 0;
    g_lane_up = 0;
}

static int core_call_c(void (*fn)(void)) {
    Job* job;
    int failed = 0;
    if (on_lane()) {
        fn();
        return 0;
    }
    job = (Job*)calloc(1, sizeof(Job));
    if (!job) return -1;
    job->kind = JOB_SYNC;
    job->cfn = fn;
    Py_BEGIN_ALLOW_THREADS
    q_lock();
    failed = !enqueue_locked(job);
    if (!failed) {
        while (!job->finished) q_wait();
    }
    q_unlock();
    Py_END_ALLOW_THREADS
    free(job);
    return failed ? -1 : 0;
}

static int native_allowed(void) {
    PyObject* result;
    int ok;
    if (!g_runtime) return 0;
    result = PyObject_CallMethod(g_runtime, "native_allowed", NULL);
    if (!result) {
        PyErr_Clear();
        return 0;
    }
    ok = PyObject_IsTrue(result) == 1;
    Py_DECREF(result);
    return ok;
}

static void* mojo_handle(int id) {
    if (id < 1 || id > MOJO_MAX) return NULL;
    return g_mojo[id - 1];
}

static int mojo_open(const char* path) {
    void* lib = NULL;
    int i;
#if defined(_WIN32)
    wchar_t wide[4096];
    if (MultiByteToWideChar(CP_UTF8, 0, path, -1, wide, 4096) <= 0) return -1;
    lib = (void*)LoadLibraryExW(wide, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
#else
    lib = dlopen(path, RTLD_NOW | RTLD_LOCAL);
#endif
    if (!lib) return -1;
    for (i = 0; i < MOJO_MAX; i++) {
        if (!g_mojo[i]) {
            g_mojo[i] = lib;
            return i + 1;
        }
    }
#if defined(_WIN32)
    FreeLibrary((HMODULE)lib);
#else
    dlclose(lib);
#endif
    return -2;
}

static void* mojo_sym(int id, const char* name) {
    void* lib = mojo_handle(id);
    if (!lib || !name) return NULL;
#if defined(_WIN32)
    return (void*)GetProcAddress((HMODULE)lib, name);
#else
    return dlsym(lib, name);
#endif
}

static void mojo_close(int id) {
    void* lib = mojo_handle(id);
    if (!lib) return;
    g_mojo[id - 1] = NULL;
#if defined(_WIN32)
    FreeLibrary((HMODULE)lib);
#else
    dlclose(lib);
#endif
}

static void mojo_close_all(void) {
    int i;
    for (i = 1; i <= MOJO_MAX; i++) mojo_close(i);
}

static void set_os_error(void) {
#if defined(_WIN32)
    PyErr_SetFromWindowsErr(GetLastError());
#else
    const char* err = dlerror();
    PyErr_SetString(PyExc_OSError, err ? err : "dlopen failed");
#endif
}

static PyObject* py_core_submit(PyObject* self, PyObject* args) {
    PyObject* fn;
    PyObject* done;
    Job* job;
    int ok;
    (void)self;
    if (!PyArg_ParseTuple(args, "OO", &fn, &done)) return NULL;
    if (!PyCallable_Check(fn) || !PyCallable_Check(done)) {
        PyErr_SetString(PyExc_TypeError, "job and done must be callable");
        return NULL;
    }
    job = (Job*)calloc(1, sizeof(Job));
    if (!job) return PyErr_NoMemory();
    job->kind = JOB_PY;
    job->fn = fn;
    job->done = done;
    Py_INCREF(fn);
    Py_INCREF(done);
    q_lock();
    ok = enqueue_locked(job);
    q_unlock();
    if (!ok) {
        Py_DECREF(fn);
        Py_DECREF(done);
        free(job);
        PyErr_SetString(PyExc_RuntimeError, "python core lane is not accepting jobs");
        return NULL;
    }
    Py_RETURN_NONE;
}

static PyObject* py_core_info(PyObject* self, PyObject* args) {
    unsigned long lane = 0;
    (void)self;
    (void)args;
#if defined(_WIN32)
    lane = (unsigned long)g_lane_tid;
#elif defined(__linux__)
    lane = (unsigned long)g_lane_os_id;
#endif
    return Py_BuildValue("{s:i,s:i,s:i,s:i,s:k}", "index", g_core, "processors", g_ncpu, "reserved", g_reserve,
        "pinned", g_core >= 0 ? 1 : 0, "lane_id", lane);
}

static PyObject* py_on_core(PyObject* self, PyObject* args) {
    (void)self;
    (void)args;
    if (on_lane()) Py_RETURN_TRUE;
    Py_RETURN_FALSE;
}

static PyObject* py_reserve(PyObject* self, PyObject* args) {
    (void)self;
    (void)args;
    core_reserve();
    Py_RETURN_NONE;
}

static PyObject* py_mojo_load(PyObject* self, PyObject* args) {
    const char* path;
    int handle;
    void* init;
    (void)self;
    if (!PyArg_ParseTuple(args, "s", &path)) return NULL;
    if (!native_allowed()) {
        PyErr_SetString(PyExc_PermissionError, "mojo libraries load only from a trusted python resource");
        return NULL;
    }
    handle = mojo_open(path);
    if (handle == -2) {
        PyErr_SetString(PyExc_RuntimeError, "too many mojo libraries");
        return NULL;
    }
    if (handle < 0) {
        set_os_error();
        return NULL;
    }
    init = mojo_sym(handle, "initialize_runtime");
    if (init && core_call_c((void (*)(void))init) != 0) {
        PyErr_SetString(PyExc_RuntimeError, "initialize_runtime did not run on the core lane");
        return NULL;
    }
    return PyLong_FromLong(handle);
}

static PyObject* py_mojo_symbol(PyObject* self, PyObject* args) {
    int handle;
    const char* name;
    void* addr;
    (void)self;
    if (!PyArg_ParseTuple(args, "is", &handle, &name)) return NULL;
    if (!native_allowed()) {
        PyErr_SetString(PyExc_PermissionError, "mojo symbols are available to trusted python resources");
        return NULL;
    }
    addr = mojo_sym(handle, name);
    return PyLong_FromUnsignedLongLong((unsigned long long)(uintptr_t)addr);
}

static PyObject* py_mojo_close(PyObject* self, PyObject* args) {
    int handle;
    (void)self;
    if (!PyArg_ParseTuple(args, "i", &handle)) return NULL;
    if (!native_allowed()) {
        PyErr_SetString(PyExc_PermissionError, "mojo libraries close only from a trusted python resource");
        return NULL;
    }
    mojo_close(handle);
    Py_RETURN_NONE;
}

static PyMethodDef g_methods[] = {
    {"core_submit", py_core_submit, METH_VARARGS, "Queue a callable on the pinned core."},
    {"core_info", py_core_info, METH_NOARGS, "Core lane placement."},
    {"on_core", py_on_core, METH_NOARGS, "True on the pinned lane."},
    {"reserve", py_reserve, METH_NOARGS, "Move other threads off the lane core."},
    {"mojo_load", py_mojo_load, METH_VARARGS, "Load a Mojo/C shared library."},
    {"mojo_symbol", py_mojo_symbol, METH_VARARGS, "Look up an exported symbol."},
    {"mojo_close", py_mojo_close, METH_VARARGS, "Unload a library opened by mojo_load."},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef g_moduledef = {
    PyModuleDef_HEAD_INIT,
    "_nodemp",
    "NodeMP Python host bridge",
    -1,
    g_methods,
};

static PyObject* PyInit__nodemp(void) {
    PyObject* module = PyModule_Create(&g_moduledef);
#ifdef Py_GIL_DISABLED
    if (module) PyUnstable_Module_SetGIL(module, Py_MOD_GIL_NOT_USED);
#endif
    return module;
}

static int add_sys_path(const char* dir, int front) {
    PyObject* sys;
    PyObject* path;
    PyObject* item;
    int rc;
    sys = PyImport_ImportModule("sys");
    if (!sys) return -1;
    path = PyObject_GetAttrString(sys, "path");
    Py_DECREF(sys);
    if (!path) return -1;
    item = PyUnicode_FromString(dir);
    if (!item) {
        Py_DECREF(path);
        return -1;
    }
    rc = front ? PyList_Insert(path, 0, item) : PyList_Append(path, item);
    Py_DECREF(item);
    Py_DECREF(path);
    return rc;
}

static int utf8_exists(const char* path) {
#if defined(_WIN32)
    wchar_t wide[4096];
    DWORD attr;
    if (!MultiByteToWideChar(CP_UTF8, 0, path, -1, wide, 4096)) return 0;
    attr = GetFileAttributesW(wide);
    return attr != INVALID_FILE_ATTRIBUTES && (attr & FILE_ATTRIBUTE_DIRECTORY) == 0;
#else
    return access(path, R_OK) == 0;
#endif
}

static int copy_str(char* out, size_t n, const char* text) {
    size_t len = strlen(text);
    if (len + 1 > n) return 0;
    memcpy(out, text, len + 1);
    return 1;
}

static int join_path(char* out, size_t n, const char* left, const char* right) {
    size_t len;
    int wrote;
    if (!left[0]) return copy_str(out, n, right);
    len = strlen(left);
    if (left[len - 1] == '/' || left[len - 1] == '\\') wrote = snprintf(out, n, "%s%s", left, right);
    else wrote = snprintf(out, n, "%s/%s", left, right);
    return wrote > 0 && (size_t)wrote < n;
}

static int module_dir(char* out, size_t n) {
#if defined(_WIN32)
    wchar_t wide[4096];
    char* slash;
    DWORD got = GetModuleFileNameW(g_module ? g_module : NULL, wide, 4096);
    if (got == 0 || got >= 4096) return 0;
    if (WideCharToMultiByte(CP_UTF8, 0, wide, -1, out, (int)n, NULL, NULL) <= 0) return 0;
#else
    Dl_info info;
    char* slash;
    if (!dladdr((void*)module_dir, &info) || !info.dli_fname) return 0;
    if (!copy_str(out, n, info.dli_fname)) return 0;
#endif
    slash = strrchr(out, '/');
#if defined(_WIN32)
    if (!slash) slash = strrchr(out, '\\');
    else {
        char* back = strrchr(out, '\\');
        if (back && back > slash) slash = back;
    }
#endif
    if (!slash) return 0;
    *slash = '\0';
    return 1;
}

static int find_root(char* out, size_t n) {
    const char* env = getenv("NODEMP_HOST_ROOT");
    char dir[4096];
    char base[4096];
    char probe[4096];
    static const char* rels[] = {"", "python", "..", "../python", "../..", "../../python", "../../../python", NULL};
    int i;
    if (env && env[0]) {
        if (!join_path(probe, sizeof probe, env, "nodemp/__init__.py")) return 0;
        if (!utf8_exists(probe)) return 0;
        return copy_str(out, n, env);
    }
    if (!module_dir(dir, sizeof dir)) return 0;
    for (i = 0; rels[i]; i++) {
        if (rels[i][0]) {
            if (!join_path(base, sizeof base, dir, rels[i])) continue;
        } else if (!copy_str(base, sizeof base, dir)) {
            continue;
        }
        if (!join_path(probe, sizeof probe, base, "nodemp/__init__.py")) continue;
        if (utf8_exists(probe)) return copy_str(out, n, base);
    }
    return 0;
}

static wchar_t* widen_utf8(const char* text) {
#if defined(_WIN32)
    int count = MultiByteToWideChar(CP_UTF8, 0, text, -1, NULL, 0);
    wchar_t* wide;
    if (count <= 0) return NULL;
    wide = (wchar_t*)malloc((size_t)count * sizeof(wchar_t));
    if (!wide) return NULL;
    if (!MultiByteToWideChar(CP_UTF8, 0, text, -1, wide, count)) {
        free(wide);
        return NULL;
    }
    return wide;
#else
    size_t count = mbstowcs(NULL, text, 0);
    wchar_t* wide;
    if (count == (size_t)-1) return NULL;
    wide = (wchar_t*)malloc((count + 1) * sizeof(wchar_t));
    if (!wide) return NULL;
    if (mbstowcs(wide, text, count + 1) == (size_t)-1) {
        free(wide);
        return NULL;
    }
    return wide;
#endif
}

static void add_env_paths(void) {
    const char* extra = getenv("NODEMP_PYTHONPATH");
    char buf[8192];
    char* cursor;
#if defined(_WIN32)
    const char sep = ';';
#else
    const char sep = ':';
#endif
    if (!extra || !extra[0]) return;
    snprintf(buf, sizeof buf, "%s", extra);
    cursor = buf;
    while (*cursor) {
        char* mark = strchr(cursor, sep);
        if (mark) *mark = '\0';
        if (*cursor) add_sys_path(cursor, 0);
        if (!mark) break;
        cursor = mark + 1;
    }
}

static int host_load(const char* name, const char* dir, const char* entry, void* user) {
    PyGILState_STATE gs;
    PyObject* result;
    int rc = -1;
    (void)user;
    if (!g_runtime) return -1;
    gs = PyGILState_Ensure();
    result = PyObject_CallMethod(g_runtime, "load_resource", "sss", name ? name : "", dir ? dir : "", entry ? entry : "");
    if (!result) log_current_error("python resource failed to load");
    else {
        rc = (int)PyLong_AsLong(result);
        if (PyErr_Occurred()) {
            log_current_error("python resource failed to load");
            rc = -1;
        }
        Py_DECREF(result);
    }
    PyGILState_Release(gs);
    return rc;
}

static void host_unload(const char* name, void* user) {
    PyGILState_STATE gs;
    PyObject* result;
    (void)user;
    if (!g_runtime) return;
    gs = PyGILState_Ensure();
    result = PyObject_CallMethod(g_runtime, "unload_resource", "s", name ? name : "");
    if (!result) log_current_error("python resource failed to unload");
    else Py_DECREF(result);
    PyGILState_Release(gs);
}

static void shutdown_python(void) {
    if (g_saved) {
        PyEval_RestoreThread(g_saved);
        g_saved = NULL;
    }
    if (g_api && g_api->unregister_language_host &&
        g_api->struct_size >= (uint32_t)(offsetof(NodeApi, unregister_language_host) + sizeof(void*))) {
        g_api->unregister_language_host("python");
    }
    Py_FinalizeEx();
    mojo_close_all();
    g_runtime = NULL;
}

#if defined(_WIN32)
BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID reserved) {
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = module;
        DisableThreadLibraryCalls(module);
    }
    return TRUE;
}
#endif

NODE_PLUGIN_ABI()

NODE_EXPORT int node_plugin_init(const NodeApi* api) {
    PyConfig config;
    PyStatus status;
    wchar_t* home = NULL;
    const char* home_env;
    char root[4096];
    char banner[512];
    char version[160];
    const char* full;
    size_t i;
    static NodeLanguageHost host;
    g_api = api;
    if (!api || (api->abi_version >> 16) != NODE_ABI_VERSION_MAJOR) return -1;
    if (api->struct_size < (uint32_t)(offsetof(NodeApi, register_language_host) + sizeof(void*))) {
        log_err("pyhost needs a server that provides register_language_host");
        return -1;
    }
    if (!find_root(root, sizeof root)) {
        log_err("pyhost could not find the nodemp package; set NODEMP_HOST_ROOT");
        return -1;
    }
    if (PyImport_AppendInittab("_nodemp", PyInit__nodemp) < 0) {
        log_err("failed to register the _nodemp builtin");
        return -1;
    }
    PyConfig_InitPythonConfig(&config);
    config.install_signal_handlers = 0;
    config.parse_argv = 0;
    config.configure_c_stdio = 0;
    config.buffered_stdio = 1;
    home_env = getenv("NODEMP_PYTHONHOME");
    if (home_env && home_env[0]) {
        home = widen_utf8(home_env);
        if (!home) {
            PyConfig_Clear(&config);
            log_err("NODEMP_PYTHONHOME is not valid text");
            return -1;
        }
        status = PyConfig_SetString(&config, &config.home, home);
        free(home);
        if (PyStatus_Exception(status)) {
            char buf[512];
            snprintf(buf, sizeof buf, "python home rejected: %s", status.err_msg ? status.err_msg : "");
            PyConfig_Clear(&config);
            log_err(buf);
            return -1;
        }
    }
    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status)) {
        char buf[512];
        snprintf(buf, sizeof buf, "python init failed: %s", status.err_msg ? status.err_msg : "");
        PyConfig_Clear(&config);
        log_err(buf);
        return -1;
    }
    PyConfig_Clear(&config);
    if (Py_Version < 0x030E0000UL) {
        snprintf(banner, sizeof banner, "pyhost requires Python 3.14 or newer, found %.180s", Py_GetVersion());
        log_err(banner);
        Py_FinalizeEx();
        return -1;
    }
    if (add_sys_path(root, 1) < 0) {
        log_current_error("failed to add the nodemp package to sys.path");
        Py_FinalizeEx();
        return -1;
    }
    add_env_paths();
    g_runtime = PyImport_ImportModule("nodemp.runtime");
    if (!g_runtime) {
        log_current_error("failed to import nodemp.runtime");
        Py_FinalizeEx();
        return -1;
    }
    {
        PyObject* addr = PyLong_FromUnsignedLongLong((unsigned long long)(uintptr_t)api);
        PyObject* bound = addr ? PyObject_CallMethod(g_runtime, "bind", "O", addr) : NULL;
        Py_XDECREF(addr);
        if (!bound) {
            log_current_error("failed to bind NodeApi");
            Py_DECREF(g_runtime);
            g_runtime = NULL;
            Py_FinalizeEx();
            return -1;
        }
        Py_DECREF(bound);
    }
    if (core_start() != 0) {
        log_err("python core lane failed to start");
        Py_DECREF(g_runtime);
        g_runtime = NULL;
        Py_FinalizeEx();
        return -1;
    }
    memset(&host, 0, sizeof host);
    host.struct_size = (uint32_t)sizeof(host);
    host.type_name = "python";
    host.load = host_load;
    host.unload = host_unload;
    if (api->register_language_host(&host) != 0) {
        log_err("resource type \"python\" is already registered");
        core_stop();
        Py_DECREF(g_runtime);
        g_runtime = NULL;
        Py_FinalizeEx();
        return -1;
    }
    if (api->set_interval) api->set_interval(5000u, reserve_timer, NULL);
    full = Py_GetVersion();
    for (i = 0; full && full[i] && full[i] != '\n' && i + 1 < sizeof version; i++) version[i] = full[i];
    version[i] = '\0';
    if (g_core < 0) {
        snprintf(banner, sizeof banner, "resources with type = \"python\" run on %.180s; background lane is not pinned",
            version);
    } else if (g_reserve) {
        snprintf(banner, sizeof banner,
            "resources with type = \"python\" run on %.180s; background lane is pinned to core %d and other threads are moved off it",
            version, g_core);
    } else {
        snprintf(banner, sizeof banner,
            "resources with type = \"python\" run on %.180s; background lane is pinned to core %d", version, g_core);
    }
    log_line(banner);
    g_saved = PyEval_SaveThread();
    g_live = 1;
    return 0;
}

NODE_EXPORT void node_plugin_shutdown(void) {
    if (!g_live) return;
    g_live = 0;
    core_stop();
    shutdown_python();
    g_api = NULL;
}
