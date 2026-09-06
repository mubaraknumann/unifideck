"""Services sub-package — Layer-5 orchestration.

The services layer sits between the Layer-4 stores (which speak the
specific protocols of each store : Epic, GOG, Ubisoft, Amazon, Microsoft)
and the Layer-6 RPC mixins (which expose the plugin's API to the JS
frontend).

Each service encapsulates cross-cutting concerns that no single store
should own : downloading, launching, cloud-saving, security auditing,
playtime tracking, etc. Services are constructed by the bootstrap
sub-package at plugin boot and injected into stores via
``store_injector.py`` where applicable.

This ``__init__`` is intentionally empty — callers reach services
through the ``ServiceContainer`` exposed by ``bootstrap.container``.
"""
