"""/api/v1: the mobile app's API (#557, docs/mobile-api.md section 3.2). The major version is in the path; changes
within v1 are additive only. The OpenAPI 3.1 spec is api/openapi/v1/ and tests/test_api_v1.py holds the routes to it
(routes.py lists what is served, the spec's paths list what is promised, and they must be the same set)."""

API_VERSION = "1.0"
