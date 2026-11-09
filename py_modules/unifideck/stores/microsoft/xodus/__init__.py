"""stores/microsoft/xodus — Xbox PC games through our own Xodus build.

The plugin half of the Xbox PC feature. ``xbox_pc.XboxPc`` is the facade
that ``MicrosoftStore`` builds. It owns the component install (backend and
runtime), the Xodus sign-in, and the status the QAM shows. ``cli.XodusCli``
is the only plugin module that runs ``xodus-cli``.

The on-disk layout, locks, install records and argv live in
``launcher/xbox_pc/``, because the out-of-process launcher reads them too.

Nothing is imported here, so importing one submodule never loads the rest.
"""
