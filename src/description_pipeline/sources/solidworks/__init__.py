"""SolidWorks source-adapter package.

The adapter is consumed through its leaf modules (``freeze``, ``scene``,
``discovery``, ...); this initializer deliberately carries no eager imports so
the import closure stays honest - the native capture/normalization domain is
loaded only when a caller imports it, never as a side effect of importing the
package.  Verification can therefore share the pure leaf modules it needs
without pulling the producer chain in.
"""
