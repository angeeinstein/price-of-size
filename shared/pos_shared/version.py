"""Release and API versions.

`API_VERSION` follows "major.minor". A worker and a coordinator are compatible
when their major versions match and the worker's minor is not newer than the
coordinator's (the coordinator only adds to the API within one major version).
"""

__version__ = "0.1.2"
# 1.1: signing key in enrollment, GET /api/v1/nodes/signing-key, releases, lease release
API_VERSION = "1.1"

API_VERSION_HEADER = "X-POS-API-Version"


def parse_api_version(value: str) -> tuple[int, int]:
    major, _, minor = value.partition(".")
    return int(major), int(minor or 0)


def is_compatible(worker_api: str, coordinator_api: str) -> bool:
    try:
        w_major, w_minor = parse_api_version(worker_api)
        c_major, c_minor = parse_api_version(coordinator_api)
    except ValueError:
        return False
    return w_major == c_major and w_minor <= c_minor
