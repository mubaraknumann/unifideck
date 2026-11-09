"""Xbox PC titles (owned and Game Pass) in the Microsoft library.

``library.XboxPcLibrary`` is the entry point; ``MicrosoftStore.get_library``
calls its ``compose``. The other modules are its parts: ``entitlements``
(which titles the account may use), ``package_plan`` (which package Xodus
would install), ``rows`` (titles as library rows), ``installed`` (the install
records) and ``snapshot`` (the last good list).
"""
