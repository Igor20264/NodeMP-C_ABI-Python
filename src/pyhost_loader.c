/* Thin module the server loads from modules/pyhost.dll.
 *
 * The interpreter and pyhost_core.dll live in modules/python_node, one level
 * down, so the server does not treat python314.dll as a Node module.
 * Loading the core by an absolute path makes Windows look for python314.dll
 * in that same folder.
 */

#include "node.h"

#include <stdio.h>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <dlfcn.h>
#include <limits.h>
#include <unistd.h>
#endif

#if defined(_WIN32)
static HMODULE g_core;
#else
static void* g_core;
#endif

typedef uint32_t (*abi_fn)(void);
typedef int (*init_fn)(const NodeApi*);
typedef void (*shutdown_fn)(void);

static abi_fn g_abi;
static init_fn g_init;
static shutdown_fn g_shutdown;

#if defined(_WIN32)
static int ensure_core(void) {
    wchar_t self[4096];
    wchar_t path[4096];
    wchar_t* slash;
    HMODULE mod = NULL;
    DWORD got;
    if (g_core) return 1;
    if (!GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
            (LPCWSTR)ensure_core, &mod) || !mod) {
        return 0;
    }
    got = GetModuleFileNameW(mod, self, 4096);
    if (got == 0 || got >= 4096) return 0;
    slash = wcsrchr(self, L'\\');
    if (!slash) slash = wcsrchr(self, L'/');
    if (!slash) return 0;
    *slash = L'\0';
    if (swprintf(path, 4096, L"%s\\python_node\\pyhost_core.dll", self) < 0) return 0;
    g_core = LoadLibraryExW(path, NULL, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (!g_core) {
        fprintf(stderr, "pyhost: could not load modules\\python_node\\pyhost_core.dll (%lu)\n", GetLastError());
        return 0;
    }
    g_abi = (abi_fn)GetProcAddress(g_core, "node_plugin_abi");
    g_init = (init_fn)GetProcAddress(g_core, "node_plugin_init");
    g_shutdown = (shutdown_fn)GetProcAddress(g_core, "node_plugin_shutdown");
    if (!g_abi || !g_init || !g_shutdown) {
        fprintf(stderr, "pyhost: python_node\\pyhost_core.dll is missing the plugin exports\n");
        return 0;
    }
    return 1;
}
#else
static int ensure_core(void) {
    char self[PATH_MAX];
    char path[PATH_MAX];
    ssize_t got;
    if (g_core) return 1;
    got = readlink("/proc/self/exe", self, sizeof self - 1);
    (void)got;
    snprintf(path, sizeof path, "python_node/pyhost_core.so");
    g_core = dlopen(path, RTLD_NOW);
    if (!g_core) return 0;
    g_abi = (abi_fn)dlsym(g_core, "node_plugin_abi");
    g_init = (init_fn)dlsym(g_core, "node_plugin_init");
    g_shutdown = (shutdown_fn)dlsym(g_core, "node_plugin_shutdown");
    return g_abi && g_init && g_shutdown;
}
#endif

NODE_EXPORT uint32_t node_plugin_abi(void) {
    if (!ensure_core()) return 0;
    return g_abi();
}

NODE_EXPORT int node_plugin_init(const NodeApi* api) {
    if (!ensure_core()) return -1;
    return g_init(api);
}

NODE_EXPORT void node_plugin_shutdown(void) {
    if (ensure_core()) g_shutdown();
}
