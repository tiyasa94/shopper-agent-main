"""Contracts for the consolidated evaluation command."""

from evaluation import cli


def test_help_lists_preserved_evaluation_workflows(capsys) -> None:
    assert cli.main(["--help"]) == 0

    output = capsys.readouterr().out
    for command in {
        "behavioral",
        "retrieval",
        "replay-csv",
        "review",
        "chat",
        "inspect-artifact",
        "curate-external",
        "curate-uat",
        "snapshot-paired",
        "snapshot-standalone",
    }:
        assert command in output


def test_unknown_command_fails_without_importing_a_workflow(capsys) -> None:
    assert cli.main(["not-a-command"]) == 2
    assert "unknown evaluation command" in capsys.readouterr().err
