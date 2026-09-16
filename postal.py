"""Extract U.S. mailing ZIPs without matching unrelated address numbers."""

import re

US_CODES = set("AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR VI GU AS MP AE AP AA FM MH PW".split())

ZIP_INPUT = re.compile(r"([0-9]{5})(?:[-\s]?([0-9]{4}))?\Z")
MAILING_ZIP = re.compile(
    r"(?<![A-Za-z])(?P<state>[A-Za-z]{2})[\s,]+"
    r"(?P<zip>[0-9]{5})(?:[-\s]?(?P<extension>[0-9]{4}))?"
    r"(?:[\s,]+(?:US|USA|U\.S\.|U\.S\.A\.|UNITED STATES(?: OF AMERICA)?))?"
    r"[\s.,]*\Z", re.I,
)


def normalize_zip(value):
    """Return five digits or ZIP+4, retaining meaningful leading zeros."""
    match = ZIP_INPUT.fullmatch(str(value or "").strip())
    if not match:
        raise ValueError("Enter a five-digit ZIP code, such as 34947, or a ZIP+4, such as 34947-2528.")
    return match[1] + ("-" + match[2] if match[2] else "")


def mailing_zip(address):
    """Only the postal position after a U.S. state code can supply a ZIP."""
    match = MAILING_ZIP.search(str(address or ""))
    if not match or match["state"].upper() not in US_CODES:
        return None
    return match["zip"] + ("-" + match["extension"] if match["extension"] else "")


def mailing_state(address):
    match = MAILING_ZIP.search(str(address or ""))
    return match["state"].upper() if match and match["state"].upper() in US_CODES else None
