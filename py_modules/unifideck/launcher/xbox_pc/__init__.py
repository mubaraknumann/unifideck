"""launcher/xbox_pc/ — Xbox PC titles through our own Xodus build.

The facts that the plugin and the launcher share live here: where the
components and install records are on disk, how they are locked, and the
argv and env for every Xodus command. The plugin side
(``stores/microsoft/xodus/``) installs and signs in; the launcher side runs
installed games.

Stdlib only, and Python 3.10 safe: the launcher runs these modules under the
system ``python3``. ``tests/unit/test_xbox_pc_py310.py`` enforces both.
"""
