"""The four roles.

Three loose booleans on the user row is how the old backend expressed this, and
it is why a refiller could be one forgotten check away from money data. A role
is a row, and authorisation asks the role table.

`is_guest` is deliberately absent: a guest is a STUDENT who has not registered,
which is an account kind rather than a permission. `subscription_enabled` is
absent because it is a billing concern, not an identity one.
"""

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    OWNER = "owner"
    REFILLER = "refiller"
    STUDENT = "student"
