"""Tests for labels.py: it prints the gh commands and runs them only with --apply.

Run: python3 -m unittest discover -s scripts
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

import labels


class LabelsTests(unittest.TestCase):
    def test_prints_commands_and_runs_nothing_without_apply(self) -> None:
        ran: list[list[str]] = []
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            labels.main(["--repo", "pjvjay/pantry-db"], run=ran.append)
        lines = out.getvalue().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "gh label create release:major --repo pjvjay/pantry-db --color "
                                   "b60205 --description 'Breaking change; below 1.0.0 it bumps "
                                   "the minor. Major per repo: RELEASING.md' --force")
        self.assertEqual(ran, [])

    def test_apply_runs_each_command_for_every_repo(self) -> None:
        ran: list[list[str]] = []
        with redirect_stdout(io.StringIO()):
            labels.main(["--apply"], run=ran.append)
        self.assertEqual(len(ran), 16)
        self.assertEqual({cmd[5] for cmd in ran}, set(labels.REPOS))

    def test_the_label_file_is_well_formed(self) -> None:
        names = [label["name"] for label in labels.load(labels.LABELS)]
        self.assertEqual(names, ["release:major", "release:minor", "release:patch",
                                 "release:none"])


if __name__ == "__main__":
    unittest.main()
