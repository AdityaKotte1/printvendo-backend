"""Commands run by an operator, not by a request.

A composition root beside `app/api` and `app/jobs`: it owns no tables and no
rules, and reaches every context through its services. The three roots may not
import each other -- enforced, not asked for.

Two commands live here, and both exist because of a gap that only a person with
shell access can close: there is no first admin on an empty database, and there
is no way to click through a print without a shop, a printer and some paper.
"""
