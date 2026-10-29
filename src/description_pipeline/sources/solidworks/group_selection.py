"""One native occurrence selection rule for Capture and portable validation."""

from .errors import CadError


def compose_link_group_selections(
    groups,
    leaves,
    provider_names,
):
    """Select each group through top-most native providers and uncovered leaves."""

    if not isinstance(groups, list) or not groups:
        raise CadError(
            "link_group_body_missing",
            "native groups must be a non-empty list",
        )
    leaf_rows = [str(name) for name in leaves]
    if len(set(leaf_rows)) != len(leaf_rows):
        raise CadError(
            "link_group_occurrence_duplicate",
            "the captured scene repeats an occurrence",
        )
    body_of = {}
    for group in groups:
        if not isinstance(group, dict):
            raise CadError(
                "link_group_body_unnamed",
                "a native group is not an object",
            )
        body_name = str(group.get("name") or group.get("id") or "")
        if not body_name:
            raise CadError(
                "link_group_body_unnamed",
                "every native group needs a name",
            )
        members = [str(item) for item in (group.get("components") or [])]
        if not members:
            raise CadError(
                "link_group_empty_selection",
                "a native group has no components",
                {"body": body_name},
            )
        for member in members:
            if member not in leaf_rows:
                raise CadError(
                    "link_group_member_unknown",
                    "a native group names an unknown occurrence",
                    {"body": body_name, "component": member},
                )
            if member in body_of:
                raise CadError(
                    "link_group_member_duplicate",
                    "an occurrence belongs to several native groups",
                    {"component": member, "bodies": [body_of[member], body_name]},
                )
            body_of[member] = body_name
    providers = {}
    for provider in sorted({str(name) for name in provider_names}):
        subtree = {
            leaf
            for leaf in leaf_rows
            if leaf == provider or leaf.startswith(provider + "/")
        }
        if not subtree:
            raise CadError(
                "link_group_provider_unknown",
                "an effective instance is not a captured occurrence",
            )
        owners = {body_of.get(leaf) for leaf in subtree}
        named = {owner for owner in owners if owner}
        if len(named) > 1:
            raise CadError(
                "link_group_provider_spans_bodies",
                "an effective instance spans several native groups",
                {"provider": provider, "bodies": sorted(named)},
            )
        if named and len(owners) != 1:
            raise CadError(
                "link_group_provider_partial",
                "an effective instance is only partially assigned",
                {"provider": provider, "body": sorted(named)[0]},
            )
        if named:
            providers[provider] = subtree
    selections = {}
    for group in groups:
        body_name = str(group.get("name") or group.get("id"))
        members = {str(item) for item in (group.get("components") or [])}
        inner = [
            provider
            for provider, subtree in providers.items()
            if subtree <= members
        ]
        top_most = [
            provider
            for provider in inner
            if not any(
                other != provider and provider.startswith(other + "/")
                for other in inner
            )
        ]
        covered = set()
        for provider in top_most:
            covered |= providers[provider]
        uncovered = sorted(members - covered)
        selection = sorted([*top_most, *uncovered])
        if not selection:
            raise CadError(
                "link_group_empty_selection",
                "a native group has no selectable occurrence",
            )
        selections[body_name] = {
            "selection": selection,
            "providers": sorted(top_most),
            "uncovered_leaves": uncovered,
        }
    return selections
