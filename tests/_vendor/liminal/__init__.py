"""Vendored copy of liminal's protocol package, for unit CI only (cabal#359).

The unit job can't install liminal: it is a private sibling, cloned only by the
dispatch-only integration job. tests/conftest.py puts this directory on
sys.path when liminal itself is not importable, so the handler tests that need
only `liminal.protocol` still run. VENDORED_FROM is the liminal commit the
files under protocol/ were copied from; test_vendored_liminal_protocol.py fails
when a real liminal is installed and its protocol differs from this copy.
"""

VENDORED_FROM = "2eb8876"
