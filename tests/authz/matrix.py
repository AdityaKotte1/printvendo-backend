"""Who may call each route.

One entry per (method, path). The audience set is exhaustive — anyone not named
must be refused. Later plans add, alongside this table, the test that actually
exercises each route as each audience against own/other/no scope; this file is
the single declaration those tests read.
"""

PUBLIC = "public"
STUDENT = "student"
OWNER = "owner"
REFILLER = "refiller"
ADMIN = "admin"
DEVICE = "device"

KNOWN_AUDIENCES = {PUBLIC, STUDENT, OWNER, REFILLER, ADMIN, DEVICE}

MATRIX: dict[tuple[str, str], set[str]] = {
    ("GET", "/health"): {PUBLIC},
}
