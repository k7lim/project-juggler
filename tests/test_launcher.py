import os
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_launcher_uses_python3_from_path_when_invoked_through_symlink(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "pj"
    launcher.symlink_to(PROJECT_ROOT / "scripts" / "pj")

    capture = tmp_path / "capture"
    fake_python = bin_dir / "python3"
    fake_python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$PJ_TEST_CAPTURE.args\"\n"
        "printf '%s\\n' \"$PYTHONPATH\" > \"$PJ_TEST_CAPTURE.pythonpath\"\n"
    )
    fake_python.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    env["PJ_TEST_CAPTURE"] = str(capture)
    env.pop("PYTHONPATH", None)

    subprocess.run(
        [str(launcher), "search", "zeaxanthin", "--pretty"],
        cwd=tmp_path,
        env=env,
        check=True,
    )

    assert Path(f"{capture}.args").read_text().splitlines() == [
        "-m",
        "pj",
        "search",
        "zeaxanthin",
        "--pretty",
    ]
    assert Path(f"{capture}.pythonpath").read_text().strip() == str(PROJECT_ROOT)
