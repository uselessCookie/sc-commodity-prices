import argparse
import re
import shutil
import sys
import unicodedata
from pathlib import Path

import requests


# ============================================================
# CONFIGURATION / FILE PATH
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

parser = argparse.ArgumentParser(
    description="Updates commodity prices in global.ini."
)

parser.add_argument(
    "-p",
    "--path",
    type=Path,
    help="Directory containing global.ini. Default: script directory.",
)

args = parser.parse_args()

if args.path is None:
    GLOBAL_INI = SCRIPT_DIR / "global.ini"
else:
    GLOBAL_INI = args.path / "global.ini"

UEX_API_URL = "https://api.uexcorp.space/2.0/commodities"

BACKUP_SUFFIX = ".prices.bak"

REQUEST_TIMEOUT = 30

# Number of decimal places for k/M
PRICE_DECIMALS = 2

# Only consider sellable commodities
REQUIRE_SELLABLE = True

# Manual mappings:
# global.ini key -> UEX name
#
# Example:
# MANUAL_ALIASES = {
#     "quantanium": "Quantainium",
# }
MANUAL_ALIASES = {}


# ============================================================
# NORMALIZATION
# ============================================================

def normalize_text(value: str) -> str:
    """
    Normalizes text for comparison.

    Examples:
        "Hephaest."       -> "hephaest"
        "Hephaestanite"   -> "hephaestanite"
        "Laranite (Raw)"  -> "laranite raw"
    """
    value = value.strip().lower()

    # Normalize Unicode
    value = unicodedata.normalize("NFKD", value)

    # Remove accents
    value = "".join(
        char for char in value
        if not unicodedata.combining(char)
    )

    # Normalize apostrophes etc.
    value = value.replace("’", "'")
    value = value.replace("‘", "'")
    value = value.replace("“", '"')
    value = value.replace("”", '"')

    # Replace non-alphanumeric characters with spaces
    value = re.sub(r"[^a-z0-9]+", " ", value)

    # Remove multiple spaces
    value = re.sub(r"\s+", " ", value)

    return value.strip()


def compact_text(value: str) -> str:
    """
    Removes spaces for an additional comparison.
    """
    return normalize_text(value).replace(" ", "")


# ============================================================
# PRICE FORMAT
# ============================================================

def format_price(value) -> str:
    """
    Formats UEX prices, e.g.:

        850     -> 850
        1871    -> 1.87k
        8495    -> 8.5k
        15000   -> 15k
        1250000 -> 1.25M
    """

    try:
        price = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid price: {value!r}")

    if price < 1000:
        return str(int(round(price)))

    if price < 1_000_000:
        result = price / 1000
        text = f"{result:.{PRICE_DECIMALS}f}".rstrip("0").rstrip(".")
        return f"{text}k"

    result = price / 1_000_000
    text = f"{result:.{PRICE_DECIMALS}f}".rstrip("0").rstrip(".")
    return f"{text}M"


# ============================================================
# REMOVE OLD PRICES
# ============================================================

PRICE_SUFFIX_RE = re.compile(
    r"""
    \s+
    (?:
        \d+(?:\.\d+)?[kKmM]
        |
        \d+
    )
    $
    """,
    re.VERBOSE,
)

PRICE_SUFFIX_RE = re.compile(
    r"\s+¤?\d+(?:\.\d+)?[kKmM]?\s*$"
)


def remove_old_price(value: str) -> str:
    """
    Removes an existing price at the end of the value.

    Example:
        "Hephaest. 15k" -> "Hephaest."
        "Laranite 8.5k" -> "Laranite"
    """
    return PRICE_SUFFIX_RE.sub("", value).rstrip()


# ============================================================
# global.ini PARSING
# ============================================================

GLOBAL_COMMODITY_RE = re.compile(
    r"^(\s*)items_commodities_([A-Za-z0-9_]+)(=)(.*?)(\r?\n)?$"
)


def extract_global_commodity_key(line: str):
    """
    Detects only the actual main commodity lines.

    This is NOT applied to lines such as:

        items_commodities_hephaestanite_desc=...
        items_commodities_hephaestanite_raw,P=...

    Returns:
        (key, value)

    or:
        None
    """

    match = GLOBAL_COMMODITY_RE.match(line)

    if not match:
        return None

    key = match.group(2)
    value = match.group(4)

    # Never modify descriptions
    if key.lower().endswith("_desc"):
        return None

    # Never modify raw variants
    if key.lower().endswith("_raw"):
        return None

    return key, value


# ============================================================
# UEX API
# ============================================================

def load_uex_commodities():
    print("Loading commodity data from UEX...")

    try:
        response = requests.get(
            UEX_API_URL,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print()
        print("ERROR: Could not retrieve the UEX API.")
        print(exc)
        sys.exit(1)

    try:
        payload = response.json()
    except ValueError as exc:
        print()
        print("ERROR: UEX API did not return valid JSON.")
        print(exc)
        sys.exit(1)

    # Depending on the API version, the list may be directly
    # contained in the response or inside "data".
    if isinstance(payload, list):
        commodities = payload

    elif isinstance(payload, dict):
        commodities = payload.get("data")

        if commodities is None:
            commodities = payload.get("commodities")

    else:
        commodities = None

    if not isinstance(commodities, list):
        print()
        print("ERROR: No commodity list found in the UEX response.")
        print()
        print(payload)
        sys.exit(1)

    print(f"UEX: Loaded {len(commodities)} commodities.")

    return commodities


# ============================================================
# UEX INDEX
# ============================================================

def build_uex_indexes(commodities):
    """
    Creates search indexes by:

        - exact name
        - normalized name
        - compact name
        - code
    """

    by_name = {}
    by_normalized_name = {}
    by_compact_name = {}
    by_code = {}

    for commodity in commodities:
        if not isinstance(commodity, dict):
            continue

        name = commodity.get("name")
        code = commodity.get("code")

        if name:
            by_name.setdefault(name.lower(), []).append(commodity)

            normalized = normalize_text(name)

            if normalized:
                by_normalized_name.setdefault(
                    normalized,
                    [],
                ).append(commodity)

            compact = compact_text(name)

            if compact:
                by_compact_name.setdefault(
                    compact,
                    [],
                ).append(commodity)

        if code:
            code_normalized = normalize_text(str(code))

            if code_normalized:
                by_code.setdefault(
                    code_normalized,
                    [],
                ).append(commodity)

    return (
        by_name,
        by_normalized_name,
        by_compact_name,
        by_code,
    )


# ============================================================
# SELECT UEX COMMODITY
# ============================================================

def is_normal_commodity(commodity):
    """
    Prefers normal/refined commodities over raw variants.

    Important:
    For example, Laranite exists as both:

        Laranite
        Laranite (Raw)

    The raw version does not have a normal selling price.
    """

    if commodity.get("is_raw") in (1, "1", True):
        return False

    return True


def choose_best_candidate(candidates):
    """
    Selects the normal commodity when multiple UEX matches exist.
    """

    if not candidates:
        return None

    # First prefer non-raw entries
    non_raw = [
        item
        for item in candidates
        if is_normal_commodity(item)
    ]

    if len(non_raw) == 1:
        return non_raw[0]

    if len(non_raw) > 1:
        # If multiple entries remain, prefer
        # an entry that is actually sellable.
        sellable = [
            item
            for item in non_raw
            if item.get("is_sellable") in (1, "1", True)
            and float(item.get("price_sell") or 0) > 0
        ]

        if len(sellable) == 1:
            return sellable[0]

        # Not unambiguous
        return None

    # Only raw entries available
    return None


def find_uex_commodity(
    global_key,
    current_value,
    indexes,
):
    """
    Searches for the matching UEX commodity.

    Priority:

        1. MANUAL_ALIASES
        2. global.ini key
        3. Code
        4. Current display name
        5. Abbreviation/prefix match

    The global.ini key is intentionally the most important source.

    This makes the following work, for example:

        items_commodities_hephaestanite=Hephaest.

    even though UEX calls it "Hephaestanite".
    """

    (
        by_name,
        by_normalized_name,
        by_compact_name,
        by_code,
    ) = indexes

    global_key_normalized = normalize_text(global_key)
    global_key_compact = compact_text(global_key)

    current_value_clean = remove_old_price(current_value)
    current_value_normalized = normalize_text(current_value_clean)
    current_value_compact = compact_text(current_value_clean)

    # --------------------------------------------------------
    # 1. Manual aliases
    # --------------------------------------------------------

    alias = MANUAL_ALIASES.get(global_key.lower())

    if alias:
        alias_normalized = normalize_text(alias)

        candidates = by_normalized_name.get(
            alias_normalized,
            [],
        )

        candidate = choose_best_candidate(candidates)

        if candidate:
            return candidate, "manual alias"

    # --------------------------------------------------------
    # 2. global.ini key -> UEX Name
    # --------------------------------------------------------

    candidates = by_normalized_name.get(
        global_key_normalized,
        [],
    )

    candidate = choose_best_candidate(candidates)

    if candidate:
        return candidate, "global key"

    # Compact Match
    candidates = by_compact_name.get(
        global_key_compact,
        [],
    )

    candidate = choose_best_candidate(candidates)

    if candidate:
        return candidate, "global key compact"

    # --------------------------------------------------------
    # 3. Try global.ini key as UEX code
    # --------------------------------------------------------

    candidates = by_code.get(
        global_key_normalized,
        [],
    )

    candidate = choose_best_candidate(candidates)

    if candidate:
        return candidate, "UEX code"

    # --------------------------------------------------------
    # 4. Try current display name
    # --------------------------------------------------------

    candidates = by_normalized_name.get(
        current_value_normalized,
        [],
    )

    candidate = choose_best_candidate(candidates)

    if candidate:
        return candidate, "display name"

    candidates = by_compact_name.get(
        current_value_compact,
        [],
    )

    candidate = choose_best_candidate(candidates)

    if candidate:
        return candidate, "display name compact"

    # --------------------------------------------------------
    # 5. Prefix match
    #
    # Example:
    #
    # global.ini:
    #     Hephaest.
    #
    # UEX:
    #     Hephaestanite
    #
    # --------------------------------------------------------

    if current_value_normalized:
        prefix_matches = []

        for name_normalized, items in by_normalized_name.items():
            if not name_normalized:
                continue

            # Abbreviations must have at least 5 characters
            # to avoid too many false positives.
            if len(current_value_normalized) >= 5:
                if name_normalized.startswith(
                    current_value_normalized
                ):
                    prefix_matches.extend(items)

        candidate = choose_best_candidate(prefix_matches)

        if candidate:
            return candidate, "display name prefix"

    return None, None


# ============================================================
# SELLING PRICE
# ============================================================

def get_sell_price(commodity):
    """
    Returns the UEX price_sell value.

    price_buy is explicitly NOT used.
    """

    if REQUIRE_SELLABLE:
        if commodity.get("is_sellable") not in (1, "1", True):
            return None, "not sellable"

    price = commodity.get("price_sell")

    if price is None:
        return None, "no price_sell"

    try:
        price = float(price)
    except (TypeError, ValueError):
        return None, "invalid price_sell"

    if price <= 0:
        return None, "price_sell is 0"

    return price, None


# ============================================================
# MAIN PROGRAM
# ============================================================

def main():

    print("=" * 70)
    print("Star Citizen global.ini -> UEX Commodity Prices")
    print("=" * 70)
    print()

    if not GLOBAL_INI.exists():
        print(f"ERROR: File not found: {GLOBAL_INI}")
        sys.exit(1)

    # --------------------------------------------------------
    # Load UEX
    # --------------------------------------------------------

    commodities = load_uex_commodities()

    indexes = build_uex_indexes(commodities)

    print()

    # --------------------------------------------------------
    # Read global.ini
    #
    # utf-8-sig accepts both:
    #
    #   UTF-8
    #   UTF-8 with BOM
    #
    # When writing, normal UTF-8 is used afterward
    # WITHOUT a BOM.
    # --------------------------------------------------------

    try:
        with open(
            GLOBAL_INI,
            "r",
            encoding="utf-8-sig",
            newline="",
        ) as file:
            content = file.read()

    except UnicodeDecodeError as exc:
        print()
        print("ERROR: global.ini is not valid UTF-8.")
        print()
        print(exc)
        print()
        print(
            "The file must be saved as UTF-8."
        )
        sys.exit(1)

    # --------------------------------------------------------
    # Backup
    # --------------------------------------------------------

    backup_path = GLOBAL_INI.with_name(
        GLOBAL_INI.name + BACKUP_SUFFIX
    )

    shutil.copy2(
        GLOBAL_INI,
        backup_path,
    )

    print(f"Backup created: {backup_path}")

    # --------------------------------------------------------
    # Analyze lines
    #
    # Important:
    # We use splitlines() and then create
    # ALL line endings ourselves using \n.
    #
    # This guarantees Unix/LF output.
    # --------------------------------------------------------

    lines = content.splitlines()
    updated_lines = []
    updated_count = 0

    not_found = []
    not_sellable = []
    no_price = []
    ambiguous = []

    # --------------------------------------------------------
    # Process each line
    # --------------------------------------------------------

    for line in lines:

        parsed = extract_global_commodity_key(line)

        if parsed is None:
            updated_lines.append(line)
            continue

        global_key, current_value = parsed

        # Only modify the normal commodity field.
        #
        # Example:
        #
        # items_commodities_hephaestanite=Hephaest.
        #
        # becomes:
        #
        # items_commodities_hephaestanite=Hephaest. 15k
        #

        commodity, match_method = find_uex_commodity(
            global_key,
            current_value,
            indexes,
        )

        if commodity is None:
            not_found.append(
                (
                    global_key,
                    current_value,
                    "no matching UEX commodity",
                )
            )

            updated_lines.append(line)
            continue

        price, reason = get_sell_price(commodity)

        if price is None:

            if reason == "not sellable":
                not_sellable.append(
                    (
                        global_key,
                        current_value,
                        commodity.get("name", "?"),
                        reason,
                    )
                )

            else:
                no_price.append(
                    (
                        global_key,
                        current_value,
                        commodity.get("name", "?"),
                        reason,
                    )
                )

            updated_lines.append(line)
            continue

        formatted_price = format_price(price)

        # Remove old price if one already exists
        clean_value = remove_old_price(
            current_value
        )

        new_value = (
            f"{clean_value} ¤{formatted_price}"
        )

        # Preserve original indentation and key structure
        indent_match = re.match(
            r"^(\s*)",
            line,
        )

        indent = (
            indent_match.group(1)
            if indent_match
            else ""
        )

        new_line = (
            f"{indent}"
            f"items_commodities_{global_key}"
            f"={new_value}"
        )

        updated_lines.append(new_line)

        updated_count += 1

        print(
            f"UPDATE: "
            f"{global_key:<45} "
            f"{price:g} -> {formatted_price:<8} "
            f"({commodity.get('name', '?')})"
        )

    # --------------------------------------------------------
    # Write output
    #
    # IMPORTANT:
    #
    # newline="\n" guarantees Unix line endings.
    #
    # encoding="utf-8" writes actual UTF-8 without a BOM.
    # --------------------------------------------------------

    output_content = "\n".join(updated_lines) + "\n"

    try:
        with open(
            GLOBAL_INI,
            "w",
            encoding="utf-8-sig",
            newline="\n",
        ) as file:
            file.write(output_content)

    except UnicodeEncodeError as exc:
        print()
        print("ERROR while writing the UTF-8 file.")
        print(exc)
        sys.exit(1)

    # ========================================================
    # SUMMARY
    # ========================================================

    print()
    print("=" * 70)
    print("DONE")
    print("=" * 70)

    print()
    print(f"Updated commodities: {updated_count}")

    # --------------------------------------------------------
    # Not found
    # --------------------------------------------------------

    if not_found:
        print()
        print(
            f"NOT FOUND ({len(not_found)}):"
        )

        for (
            global_key,
            current_value,
            reason,
        ) in not_found:

            print(
                f"  - {global_key}"
                f" = {current_value!r}"
                f" -> {reason}"
            )

    # --------------------------------------------------------
    # Not sellable
    # --------------------------------------------------------

    if not_sellable:
        print()
        print(
            f"NOT SELLABLE ({len(not_sellable)}):"
        )

        for (
            global_key,
            current_value,
            uex_name,
            reason,
        ) in not_sellable:

            print(
                f"  - {global_key}"
                f" = {current_value!r}"
                f" -> UEX: {uex_name}"
                f" ({reason})"
            )

    # --------------------------------------------------------
    # No selling price
    # --------------------------------------------------------

    if no_price:
        print()
        print(
            f"NO SELLING PRICE ({len(no_price)}):"
        )

        for (
            global_key,
            current_value,
            uex_name,
            reason,
        ) in no_price:

            print(
                f"  - {global_key}"
                f" = {current_value!r}"
                f" -> UEX: {uex_name}"
                f" ({reason})"
            )

    # --------------------------------------------------------
    # Everything successful
    # --------------------------------------------------------

    if not not_found and not not_sellable and not no_price:
        print()
        print(
            "All found commodities could be updated "
            "with a selling price."
        )

    print()
    print(f"File:     {GLOBAL_INI}")
    print(f"Backup:   {backup_path}")
    print("Encoding: UTF-8")
    print("Line endings: Unix (LF)")
    print()


if __name__ == "__main__":
    main()