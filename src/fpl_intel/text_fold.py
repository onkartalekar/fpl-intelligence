"""Shared Unicode-folding helpers for fuzzy name matching.

Issue #239: this table used to be duplicated -- once here (well, originally only
in sources/relevance.py) for matching transfer-feed player names against FPL's
own records, and a second, independently hand-written copy in
js/dashboard/core.js's `specialLetterFold` for the dashboard's player-search
boxes (added in #238 after "ode" failed to match "Ødegaard"). Two copies drift:
a letter added to one map silently isn't added to the other. This module is now
the single Python-side source of truth; relevance.py imports it instead of
keeping its own table. (core.js keeps its own copy for folding the user's typed
*query* client-side -- see that file's comment for why a JS-side fold still has
to exist independently of this one.)
"""

import unicodedata

# Some Latin letters have no NFKD decomposition into "base letter + combining
# diacritic" -- they're distinct letterforms, not accented variants of an
# ASCII letter -- so NFKD-then-ascii-encode silently drops them instead of
# transliterating them (e.g. "Nørgaard" loses the "o" entirely and becomes
# "Nrgaard", not "Norgaard"). Substitute these first so the rest of the
# pipeline sees a plausible ASCII form, matching how press/transfer-feed
# reporting typically renders them.
#
# This is a fixed, hand-picked list, not a general transliteration table --
# see #239's "Open design question" for why: a real transliteration library
# would need a new third-party dependency, which conflicts with this repo's
# stdlib-only policy (requirements.txt's numpy comment). A name using a
# special letter not on this list will still need a new entry added here.
_NON_DECOMPOSABLE_LETTERS = str.maketrans({
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th",
    "đ": "d", "Đ": "D",
    "ł": "l", "Ł": "L",
    "ß": "ss",
})


def fold_ascii(text):
    """Transliterate `text` to a plain ASCII form for fuzzy matching.

    Strips accents/diacritics (e.g. "é" -> "e") and substitutes the fixed set
    of non-decomposable special letters above (e.g. "Ø" -> "O"). Case and
    whitespace are left untouched -- callers that need those normalized too
    (e.g. `search_key` below, or relevance.py's stricter token matching)
    handle that themselves.
    """
    text = (text or "").translate(_NON_DECOMPOSABLE_LETTERS)
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def search_key(text):
    """ASCII-folded, casefolded form of `text` for substring search matching.

    Preserves whitespace and word order (unlike relevance.py's `_token`, which
    also strips all non-alphanumeric characters for exact-match comparison) so
    a multi-word query like "martin od" still matches "Martin Ødegaard" as a
    plain substring once both sides go through this same fold.
    """
    return fold_ascii(text).casefold()
