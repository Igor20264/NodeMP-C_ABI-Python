cmake_minimum_required(VERSION 3.24)
project(pyhost C)

# Point Python_ROOT_DIR at the CPython you ship (3.14 or 3.15, including a
# free-threaded build if the lane should run without the GIL).
find_package(Python 3.14...<3.16 REQUIRED COMPONENTS Development.Embed)

add_library(pyhost_core SHARED src/pyhost.c)
target_include_directories(pyhost_core PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/sdk")
target_link_libraries(pyhost_core PRIVATE Python::Python)
set_target_properties(pyhost_core PROPERTIES PREFIX "" OUTPUT_NAME "pyhost_core")

add_library(pyhost SHARED src/pyhost_loader.c)
target_include_directories(pyhost PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/sdk")
set_target_properties(pyhost PROPERTIES PREFIX "" OUTPUT_NAME "pyhost")

if(MSVC)
    target_compile_options(pyhost_core PRIVATE /std:c11 /W3 /utf-8)
    target_compile_options(pyhost PRIVATE /std:c11 /W3 /utf-8)
    set_property(TARGET pyhost_core PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")
    set_property(TARGET pyhost PROPERTY MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")
else()
    target_compile_options(pyhost_core PRIVATE -std=c11 -Wall -Wextra -Wno-unused-parameter)
    target_compile_options(pyhost PRIVATE -std=c11 -Wall -Wextra -Wno-unused-parameter)
    target_link_libraries(pyhost_core PRIVATE ${CMAKE_DL_LIBS})
    target_link_libraries(pyhost PRIVATE ${CMAKE_DL_LIBS})
endif()
