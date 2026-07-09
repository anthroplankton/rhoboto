from __future__ import annotations

from components.ui_team_register import EncoreRoleSelect
from tests.fakes import FakeRole


def test_encore_role_select_uses_defaults_for_active_stored_roles() -> None:
    select = EncoreRoleSelect(
        object(),
        roles=[
            FakeRole(id=1, name="@everyone", position=99, default=True),
            FakeRole(id=2, name="Managed", position=98, managed=True),
            FakeRole(id=10, name="General", position=10),
            FakeRole(id=11, name="Encore", position=1),
            FakeRole(id=12, name="Lead", position=20),
        ],
        encore_role_ids=[11],
    )

    assert select.min_values == 0
    assert select.max_values == 25
    assert select.disabled is False
    assert [default.id for default in select.default_values] == [11]
