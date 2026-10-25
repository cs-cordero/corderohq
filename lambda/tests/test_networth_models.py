import pytest

from corderohq.networth.models import validate_birth_year_month


class TestValidateBirthYearMonth:
    def test_accepts_year_month(self) -> None:
        assert validate_birth_year_month("1985-07") == "1985-07"

    @pytest.mark.parametrize("bad", ["1985-7", "1985", "1985-07-01", "", None, 198507])
    def test_rejects_bad_format(self, bad: object) -> None:
        with pytest.raises(ValueError, match="birthYearMonth"):
            validate_birth_year_month(bad)
