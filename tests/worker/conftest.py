"""Worker tests run under the cloud tests' guards: no network, no delete under the archive."""

from tests.cloud.conftest import _never_delete_archive, _no_network

__all__ = ["_never_delete_archive", "_no_network"]
