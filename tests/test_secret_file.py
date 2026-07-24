import stat

import pytest

from maestro.execution.secret_file import validate_secret_value, write_env_file


def test_validate_rejects_control_chars():
    for bad in ("a\nb", "a\rb", "a\x00b"):
        with pytest.raises(ValueError, match="control char"):
            validate_secret_value("K", bad)


def test_write_env_file_is_0600_and_skips_absent(tmp_path):
    d = tmp_path / "sec"
    d.mkdir(mode=0o700)
    p = write_env_file(d / "env", ["A", "MISSING"], {"A": "x"})
    assert p.read_text() == "A=x\n"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
