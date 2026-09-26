"""Evaluator for the PUB catalog's entitlement rules.

py_modules/unifideck/stores/battlenet/ownership/rules.py

Blizzard does not ship a flat "owned games" list. Each catalog fragment
carries a ``program_configuration`` whose rules are evaluated against
account facts, and the products those rules add ARE the playable library.
Treating it as a licence lookup table under-reports badly: it misses every
free-to-play and subscription title, because those match on
``game_account`` rather than ``license_id``.

Grammar, enumerated from a real 254-fragment cache on 2026-08-09:

  match:   license_id (scalar or list) | game_account{program_id}
           | flag | all_of[] | any_of[] | not{}
  actions: add_product{product_id{id,type}} | add_tag{name}
           | run_first_rule[]   (nested, first matching rule only)

``run_each_rule`` evaluates every rule; ``run_first_rule`` stops at the
first match. Both appear in the real catalog and the distinction matters —
collapsing them would grant products from mutually exclusive branches
(e.g. a Game Pass branch and a retail branch simultaneously).

An unknown match key evaluates **False**, never True: inventing ownership
is worse than missing it, and a future Blizzard match type should degrade
to "not owned" rather than granting everything.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

# Rules whose actions are all evaluated.
KEY_RUN_EACH = "run_each_rule"
# Rules where only the first match fires.
KEY_RUN_FIRST = "run_first_rule"


@dataclass(frozen=True, slots=True)
class AccountFacts:
    """What we know about the signed-in account, as the rules see it.

    ``licence_ids`` comes from the client's ``CachedData.db``.
    ``game_account_programs`` has no source: no local file records the
    user's game accounts, so ``library._presumed_facts`` fills it with every
    program in the catalog and the free-to-play set is granted on that
    presumption. ``flags`` covers the handful of ``flag`` matches in the
    catalog and has no producer at all (audit register item 51).
    """

    licence_ids: frozenset[int] = frozenset()
    game_account_programs: frozenset[str] = frozenset()
    flags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class GrantedProduct:
    """A product the rules granted, with the tags that came with it.

    ``program`` is the ``program_configuration`` key whose rules fired —
    the authoritative parent. ``product_id`` is what the rule *named*, and
    the two differ for variants: program ``WoW`` grants ``WoWPTR``, program
    ``ARIS`` grants ``ARIS_Standard``. Grouping the library by ``program``
    is what stops a PTR realm appearing as a separate game; inferring the
    parent from the id afterwards does not work, because those ids are not
    catalog entries at all.
    """

    program: str
    product_id: str
    product_type: str
    tags: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_free_to_play(self) -> bool:
        return "play_for_free" in self.tags


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _match_licence(criteria: object, facts: AccountFacts) -> bool:
    ids = [i for i in _as_list(criteria) if isinstance(i, int)]
    return any(i in facts.licence_ids for i in ids)


def _match_game_account(criteria: object, facts: AccountFacts) -> bool:
    """A game account for the named program. This is the free-to-play path."""
    for entry in _as_list(criteria):
        if not isinstance(entry, dict):
            continue
        program = entry.get("program_id")
        if isinstance(program, str) and program in facts.game_account_programs:
            return True
    return False


def _match_flag(criteria: object, facts: AccountFacts) -> bool:
    return any(f in facts.flags for f in _as_list(criteria) if isinstance(f, str))


def _match_all_of(value: object, facts: AccountFacts) -> bool:
    return all(matches(c, facts) for c in _as_list(value))


def _match_any_of(value: object, facts: AccountFacts) -> bool:
    return any(matches(c, facts) for c in _as_list(value))


def _match_not(value: object, facts: AccountFacts) -> bool:
    return not matches(value, facts)


# The grammar as data. A key absent from here evaluates False — inventing
# ownership is worse than missing it, so an unfamiliar future criterion
# degrades to "not owned" rather than granting everything.
_MATCHERS: dict[str, Callable[[object, AccountFacts], bool]] = {
    "license_id": _match_licence,
    "game_account": _match_game_account,
    "flag": _match_flag,
    "all_of": _match_all_of,
    "any_of": _match_any_of,
    "not": _match_not,
}


def matches(criteria: object, facts: AccountFacts) -> bool:
    """Evaluate one ``match`` block. Unknown keys are False, never True."""
    if not isinstance(criteria, dict) or not criteria:
        return False
    # Sibling keys within one match block are conjunctive.
    return all(
        _MATCHERS[key](value, facts) if key in _MATCHERS else False
        for key, value in criteria.items()
    )


def _action_tags(items: list[object]) -> set[str]:
    """Tags a rule applies, gathered before any product is emitted."""
    found: set[str] = set()
    for action in items:
        if not isinstance(action, dict):
            continue
        tag = action.get("add_tag")
        if isinstance(tag, dict) and isinstance(tag.get("name"), str):
            found.add(tag["name"])
    return found


def _product_key(action: dict[str, object]) -> tuple[str, str] | None:
    """The ``(product_id, type)`` an ``add_product`` action grants."""
    add = action.get("add_product")
    pid = add.get("product_id") if isinstance(add, dict) else None
    if not isinstance(pid, dict):
        return None
    ident = pid.get("id")
    if not isinstance(ident, str) or not ident:
        return None
    return ident, str(pid.get("type") or "unknown")


def _collect_actions(
    actions: object, family: str, tags: set[str], out: dict[tuple[str, str], set[str]],
    facts: AccountFacts, depth: int,
) -> None:
    """Apply one rule's actions.

    Two passes, deliberately. A rule's ``add_tag`` actions apply to every
    product that rule grants regardless of ordering, and the real catalog
    lists ``add_product`` *before* ``add_tag`` — a single ordered pass
    silently dropped ``play_for_free`` from every free-to-play title.
    """
    items = _as_list(actions)
    tags.update(_action_tags(items))
    for action in items:
        if not isinstance(action, dict):
            continue
        nested = action.get(KEY_RUN_FIRST)
        if nested is not None:
            _run(nested, family, facts, out, first_only=True, depth=depth + 1, inherited=tags)
        key = _product_key(action)
        if key is not None:
            out.setdefault(key, set()).update(tags)


# Nested run_first_rule blocks are shallow in practice; this only stops a
# malformed catalog from recursing without bound.
_MAX_RULE_DEPTH = 8


def _run(
    rules: object, family: str, facts: AccountFacts,
    out: dict[tuple[str, str], set[str]], *, first_only: bool,
    depth: int = 0, inherited: set[str] | None = None,
) -> None:
    if depth > _MAX_RULE_DEPTH:
        return
    for rule in _as_list(rules):
        if not isinstance(rule, dict):
            continue
        if not matches(rule.get("match"), facts):
            continue
        tags = set(inherited or ())
        _collect_actions(rule.get("actions"), family, tags, out, facts, depth)
        if first_only:
            return


def evaluate_program(
    family: str, config: object, facts: AccountFacts
) -> set[GrantedProduct]:
    """Evaluate one program's rules into the products it grants."""
    if not isinstance(config, dict):
        return set()
    out: dict[tuple[str, str], set[str]] = {}
    _run(config.get(KEY_RUN_EACH), family, facts, out, first_only=False)
    _run(config.get(KEY_RUN_FIRST), family, facts, out, first_only=True)
    return {
        GrantedProduct(
            program=family, product_id=pid, product_type=typ, tags=frozenset(tags)
        )
        for (pid, typ), tags in out.items()
    }


def evaluate_catalog(
    program_configurations: dict[str, object], facts: AccountFacts
) -> dict[str, frozenset[GrantedProduct]]:
    """Evaluate every program in the catalog. Returns ``{program: products}``."""
    granted: dict[str, set[GrantedProduct]] = {}
    for family, config in program_configurations.items():
        for product in evaluate_program(family, config, facts):
            granted.setdefault(product.program, set()).add(product)
    return {k: frozenset(v) for k, v in granted.items()}
