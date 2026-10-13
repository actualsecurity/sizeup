"""sizeup: fire dispatch situational display for Port Chester FD firehouses."""

__version__ = "0.0.1"   # pyproject.toml's; /healthz reports the installed one when there is one
# Every outbound request names us and a mailbox someone answers (feeds, OSM, the county, 511NY).
USER_AGENT = f"sizeup/{__version__} (Port Chester FD dispatch; contact pcfd-sizeup.cqmpy@dralias.com)"
