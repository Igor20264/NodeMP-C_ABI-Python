"""NodeMP Python 3.14 and 3.15 host package.

A resource imports nothing to see ``node``: the host puts it in the script
globals. ``from nodemp import node`` is the same object.
"""

from nodemp.api import Handle, Node, NodeError, node

__all__ = ["Handle", "Node", "NodeError", "node"]
