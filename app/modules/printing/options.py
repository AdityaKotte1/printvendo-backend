"""What a student asked for, and the three numbers that follow from it.

The backend being replaced stored print options as a JSON blob and interpreted
it in four independent places -- pricing, storage sanitising, paper accounting,
and the agent's CUPS command -- each with its own try/except and its own
defaults. Nothing forced them to agree, so what a student was charged, how much
paper was deducted, and what actually came out of the printer were three
separate opinions about the same blob.

Here the options are parsed once, normalised once, and every consumer calls the
same function to get its numbers.

**Three words, three meanings, never conflated:**

    pages        how long the document is
    impressions  how many sides get printed  -- what pricing counts
    sheets       how much paper is consumed  -- what the tray loses

For a 10-page document, 2 copies, double-sided: 10 pages, 20 impressions,
10 sheets. Charging for sheets or deducting paper by impressions is how a shop
misreads its own books.
"""

import re
from dataclasses import dataclass

from app.core.errors import BadRequest

MAX_COPIES = 50
RANGE_TOKEN = re.compile(r"^(\d+)(?:-(\d+))?$")


def parse_page_range(raw: str | None, *, total_pages: int) -> list[int]:
    """Turn "12-17,1" into a sorted, de-duplicated list of page numbers.

    Returns every page when `raw` is empty, so callers never have to special-case
    "all pages" -- it is just the full list.

    Refuses anything outside the document rather than clamping. A student asking
    for pages 5-40 of a 10-page file has made a mistake, and printing pages 5-10
    while charging for six is worse than saying so.
    """
    if raw is None or not raw.strip():
        return list(range(1, total_pages + 1))

    if total_pages < 1:
        raise BadRequest("That document has no pages to print.")

    selected: set[int] = set()

    for token in raw.split(","):
        # Strip whitespace *inside* the token too, not just around it. People
        # type "1 - 5" with spaces around the hyphen, and refusing that is
        # pedantry rather than validation.
        token = "".join(token.split())
        if not token:
            continue

        match = RANGE_TOKEN.match(token)
        if not match:
            raise BadRequest(
                f"{token!r} is not a page or page range. Use something like 1,4-6."
            )

        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start

        if start < 1 or end < 1:
            raise BadRequest("Page numbers start at 1.")
        if end < start:
            raise BadRequest(f"{token!r} runs backwards. Write it as {end}-{start}.")
        if end > total_pages:
            raise BadRequest(
                f"This document has {total_pages} page"
                f"{'s' if total_pages != 1 else ''}, so page {end} does not exist."
            )

        selected.update(range(start, end + 1))

    if not selected:
        raise BadRequest("Choose at least one page to print.")

    return sorted(selected)


def format_page_range(pages: list[int]) -> str:
    """Render a page list back as compact, ascending ranges: [1,4,5,6] -> "1,4-6".

    CUPS requires ascending order with no spaces, and a normalised form means
    the string stored, the string priced against, and the string sent to the
    printer are the same string.
    """
    if not pages:
        return ""

    parts: list[str] = []
    start = previous = pages[0]

    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        parts.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page

    parts.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(parts)


@dataclass(frozen=True)
class PrintOptions:
    """A validated, normalised print request.

    Frozen: once an order is priced against these, nothing downstream may adjust
    them. A change means a new order.
    """

    colour: bool
    duplex: bool
    copies: int
    page_range: str | None  # normalised, or None meaning every page

    @classmethod
    def create(
        cls,
        *,
        total_pages: int,
        colour: bool = False,
        duplex: bool = False,
        copies: int = 1,
        page_range: str | None = None,
    ) -> "PrintOptions":
        """Validate and normalise. The only way to build one."""
        if total_pages < 1:
            raise BadRequest("That document has no pages to print.")

        if copies < 1:
            raise BadRequest("Print at least one copy.")
        if copies > MAX_COPIES:
            raise BadRequest(
                f"{copies} copies is more than a kiosk will do in one job "
                f"(limit {MAX_COPIES}). Split it into separate jobs."
            )

        pages = parse_page_range(page_range, total_pages=total_pages)

        # Store None for "everything" rather than an explicit full range, so
        # "all pages" has one representation instead of two that must be kept
        # in step as the document changes.
        normalised = None if len(pages) == total_pages else format_page_range(pages)

        return cls(colour=colour, duplex=duplex, copies=copies, page_range=normalised)


@dataclass(frozen=True)
class Workload:
    """What a print request costs in pages, sides and paper.

    `sheets` splits in two because the two kinds are **priced differently**: a
    sheet printed on both sides is charged at the duplex rate, and a sheet
    printed on one side is charged at the single rate whatever the job asked
    for. A seven-page duplex job is three of the first and one of the second --
    the last sheet has a blank back, and charging the double rate for it charges
    for a side nobody printed.

    The split lives here rather than in the quote because this is the one
    calculation pricing, paper accounting and the device all read. Two of them
    working it out separately is how a price, a tray count and a printed job
    come to disagree.
    """

    pages: int
    selected_pages: int
    impressions: int
    sheets: int
    sheets_both_sides: int
    sheets_one_side: int


def workload(options: PrintOptions, *, total_pages: int) -> Workload:
    """The single calculation behind pricing, paper accounting and the device.

    Every consumer calls this. They cannot disagree about how much is being
    printed, because there is only one answer.
    """
    selected = len(parse_page_range(options.page_range, total_pages=total_pages))

    impressions = selected * options.copies

    # Duplex halves the paper, rounding up: an odd number of sides still leaves
    # one sheet printed on one side only. Each *copy* rounds separately --
    # copies do not share a sheet, because the back of one copy's last page is
    # not the front of the next copy's first.
    if options.duplex:
        both_sides = (selected // 2) * options.copies
        one_side = (selected % 2) * options.copies
    else:
        both_sides = 0
        one_side = impressions

    return Workload(
        pages=total_pages,
        selected_pages=selected,
        impressions=impressions,
        sheets=both_sides + one_side,
        sheets_both_sides=both_sides,
        sheets_one_side=one_side,
    )
