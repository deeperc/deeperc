"""corpus_baseline.git_dirty() — must ignore untracked files.

A bare `git status --porcelain` counts untracked scratch output (e.g. a
demo run's corpus_results/) as "dirty", so a tracked tree that exactly
matches its own commit was misreported as dirty. Fixed via
--untracked-files=no. Verdict-inert -- git_dirty feeds provenance metadata
(baseline filenames, report "git_dirty" field) only, never a checker
verdict.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

import corpus_baseline  # noqa: E402


def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def _commit_all(path, message="init"):
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)


def test_untracked_file_alone_is_not_dirty(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    _commit_all(tmp_path)

    (tmp_path / "scratch_output.json").write_text("{}\n")  # untracked, uncommitted

    assert corpus_baseline.git_dirty(cwd=tmp_path) is False


def test_modified_tracked_file_is_dirty(tmp_path):
    _init_repo(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n")
    _commit_all(tmp_path)

    tracked.write_text("v2\n")  # real change to a tracked file

    assert corpus_baseline.git_dirty(cwd=tmp_path) is True


def test_clean_tree_with_untracked_file_present_is_not_dirty(tmp_path):
    """The exact regression case: a clean tracked tree plus scratch output
    (e.g. corpus_results/ from a demo run) must not read as dirty."""
    _init_repo(tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n")
    _commit_all(tmp_path)

    scratch_dir = tmp_path / "corpus_results"
    scratch_dir.mkdir()
    (scratch_dir / "report.json").write_text("{}\n")

    assert corpus_baseline.git_dirty(cwd=tmp_path) is False
