from __future__ import annotations

from components.ui_team_register import EncoreRoleMultiSelect
from tests.fakes import FakeRole


def test_encore_role_options_are_dynamic_and_defaulted() -> None:
    select = EncoreRoleMultiSelect(
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

    options = {option.value: option for option in select.options}

    assert set(options) == {"10", "11", "12"}
    assert options["11"].default is True
    assert [option.value for option in select.options] == ["11", "12", "10"]
    assert select.disabled is False
