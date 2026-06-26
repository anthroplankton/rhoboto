from __future__ import annotations

from components.ui_team_register import (
    ENCORE_ROLE_SELECT_MAX_VALUES,
    build_encore_role_preview_embed,
    resolve_encore_roles,
)
from tests.fakes import FakeRole


def test_resolve_encore_roles_preserves_active_and_missing_order() -> None:
    resolution = resolve_encore_roles(
        [20, 99, 10],
        [
            FakeRole(id=10, name="Alpha", position=1),
            FakeRole(id=20, name="Beta", position=2),
        ],
    )

    assert [role.id for role in resolution.active_roles] == [20, 10]
    assert resolution.missing_role_ids == (99,)


def test_encore_role_preview_warns_only_when_everyone_is_selected() -> None:
    embed = build_encore_role_preview_embed(
        selected_roles=[
            FakeRole(id=111, name="@everyone", position=99, default=True),
            FakeRole(id=20, name="Encore", position=1),
        ],
        retained_missing_role_ids=(),
        guild_id=111,
    )

    field_by_name = {field.name: field.value for field in embed.fields}
    assert "⚠ Warnings" in field_by_name
    assert "@everyone" in field_by_name["⚠ Warnings"]
    assert "Google Sheets" in field_by_name["⚠ Warnings"]


def test_encore_role_preview_omits_warning_without_everyone() -> None:
    embed = build_encore_role_preview_embed(
        selected_roles=[FakeRole(id=20, name="Encore", position=1)],
        retained_missing_role_ids=(99,),
        guild_id=111,
    )

    field_by_name = {field.name: field.value for field in embed.fields}
    assert "⚠ Warnings" not in field_by_name
    assert field_by_name["Retained Missing Role IDs"] == "`99`"


def test_encore_role_select_max_values_matches_discord_limit() -> None:
    assert ENCORE_ROLE_SELECT_MAX_VALUES == 25
