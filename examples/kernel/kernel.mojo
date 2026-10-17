# Mojo extension loaded by the Python host.
#
# There is no main(). Build it next to the resource:
#
#     mojo build kernel.mojo --emit shared-lib -o kernel.pyd
#
# PyInit_kernel is the only exported symbol. The functions registered below
# run on the pinned core when the resource passes them to node.job.

from std.os import abort
from std.python import PythonObject
from std.python.bindings import PythonModuleBuilder


fn triangle(py_obj: PythonObject) raises -> PythonObject:
    var n = Int(py=py_obj)
    var total: Int = 0
    for i in range(n + 1):
        total += i
    return PythonObject(total)


@export
fn PyInit_kernel() abi("C") -> PythonObject:
    try:
        var m = PythonModuleBuilder("kernel")
        m.def_function[triangle]("triangle", docstring="Sum 0..n on the Mojo side.")
        return m.finalize()
    except e:
        abort(String("kernel init failed: ", e))
