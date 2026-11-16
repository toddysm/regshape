"""Artifact option compatibility, error channels and completion without registry I/O."""

import inspect
from collections.abc import Callable
from itertools import product

import click
import pytest
from click.testing import CliRunner

from regshape.cli.options import _resolve_artifact_reference, artifact_option
from regshape.libs.refs import parse_artifact_ref

REFERENCE = "registry.example.invalid/team/package:v1"
OTHER_REFERENCE = "registry.example.invalid/team/package:v2"
ALIASES = ("--artifact", "-a", "--image-ref", "-i")
CONFLICT = "Conflicting reference options. Supply one artifact reference."


@pytest.fixture
def runner() -> CliRunner:
    """Capture stderr separately on both minimum Click 8.1 and newer releases."""
    factory: Callable[..., CliRunner] = CliRunner
    if "mix_stderr" in inspect.signature(CliRunner).parameters:
        return factory(mix_stderr=False)
    return factory()


@pytest.fixture
def calls() -> list[str]:
    """Record command-body entry independently of any option-parsing result."""
    return []


@pytest.fixture
def command(calls: list[str]) -> click.Command:
    """Expose the real shared decorator on a side-effect-recording synthetic command."""

    @click.command("example")
    @artifact_option(help_text="Target reference with embedded registry.")
    def example(image_ref: str) -> None:
        """Receive the compatible scalar keyword after all option validation succeeds."""
        assert isinstance(image_ref, str)
        calls.append(image_ref)
        click.echo(image_ref)

    return example


@pytest.mark.parametrize(
    "arguments",
    [
        ["--artifact", REFERENCE],
        ["-a", REFERENCE],
        ["--image-ref", REFERENCE],
        ["-i", REFERENCE],
        [f"--artifact={REFERENCE}"],
        [f"-a{REFERENCE}"],
        [f"--image-ref={REFERENCE}"],
        [f"-i{REFERENCE}"],
    ],
)
def test_alias_forms_preserve_scalar_parameter(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
    arguments: list[str],
) -> None:
    """Each spelling and value syntax selects one target without runtime warnings."""
    result = runner.invoke(command, arguments)
    assert result.exit_code == 0, result.output
    assert result.stdout == REFERENCE + "\n"
    assert result.stderr == ""
    assert calls == [REFERENCE]


@pytest.mark.parametrize("first,second", list(product(ALIASES, repeat=2)))
def test_identical_repeated_aliases_resolve_once(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
    first: str,
    second: str,
) -> None:
    """Mixing or repeating any spelling with one exact value invokes the command once."""
    result = runner.invoke(command, [first, REFERENCE, second, REFERENCE])
    assert result.exit_code == 0, result.output
    assert result.stdout == REFERENCE + "\n" and result.stderr == ""
    assert calls == [REFERENCE]


@pytest.mark.parametrize("first,second", list(product(ALIASES, repeat=2)))
def test_conflicting_aliases_fail_before_command(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
    first: str,
    second: str,
) -> None:
    """Conflicts are generic usage errors, never last-value-wins command execution."""
    result = runner.invoke(command, [first, REFERENCE, second, OTHER_REFERENCE])
    assert result.exit_code == 2 and result.stdout == "" and calls == []
    assert result.stderr == (
        "Usage: example [OPTIONS]\n"
        "Try 'example --help' for help.\n\n"
        f"Error: {CONFLICT}\n"
    )
    assert REFERENCE not in result.stderr and OTHER_REFERENCE not in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--artifact"],
        ["-a"],
        ["--image-ref"],
        ["-i"],
        ["--unknown", REFERENCE],
        ["--artifact", REFERENCE, "--image-ref"],
    ],
)
def test_usage_errors_do_not_enter_command(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
    arguments: list[str],
) -> None:
    """Click retains missing-value and unknown-option diagnostics without command side effects."""
    result = runner.invoke(command, arguments)
    assert result.exit_code == 2 and result.stdout == "" and calls == []
    assert "Error:" in result.stderr


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["--artifact", REFERENCE, "--image-ref", OTHER_REFERENCE, "--help"],
    ],
)
def test_help_is_eager_and_deprecates_only_legacy_spellings(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
    arguments: list[str],
) -> None:
    """Help explains legacy deprecation without marking the shared option deprecated."""
    result = runner.invoke(command, arguments, terminal_width=100)
    assert result.exit_code == 0 and result.stderr == "" and calls == []
    normalized = " ".join(result.stdout.split())
    for alias in ALIASES:
        assert alias in normalized
    assert "ARTIFACT" in normalized and "[required]" in normalized
    assert (
        "Legacy --image-ref / -i: Deprecated in 0.2; use --artifact / -a. "
        "Retained for compatibility."
    ) in normalized
    assert not getattr(command.params[0], "deprecated", False)


def test_custom_repository_help(runner: CliRunner) -> None:
    """Repository-oriented callers can supply accurate semantics and a different metavar."""

    @click.command()
    @artifact_option(
        help_text="Repository only; suffixes are ignored.", metavar="REPOSITORY"
    )
    def repository(image_ref: str) -> None:
        """Accept the same compatible parameter despite repository-specific help."""
        click.echo(image_ref)

    result = runner.invoke(repository, ["--help"])
    assert result.exit_code == 0
    assert "REPOSITORY" in result.stdout and "ARTIFACT" not in result.stdout
    assert "Repository only; suffixes are ignored." in result.stdout
    assert " ".join(result.stdout.split()).count("Deprecated in 0.2") == 1


@pytest.mark.parametrize(
    "reference", ["", "not-a-registry:tag", "registry.example.invalid/repo@bad"]
)
def test_option_does_not_change_reference_grammar(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
    reference: str,
) -> None:
    """Malformed or empty explicit values reach the caller's existing parser unchanged."""
    result = runner.invoke(command, ["--artifact", reference])
    assert result.exit_code == 0 and calls == [reference]
    assert result.stdout == reference + "\n" and result.stderr == ""


@pytest.mark.parametrize(
    "other",
    [
        "registry.example.invalid/team/package",
        "registry.example.invalid/team/package@sha256:" + "a" * 64,
        REFERENCE + " ",
    ],
)
def test_values_are_compared_without_normalization(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
    other: str,
) -> None:
    """Possible remote equivalence or whitespace never turns differing inputs into one value."""
    result = runner.invoke(command, ["-a", REFERENCE, "-i", other])
    assert result.exit_code == 2 and calls == [] and result.stdout == ""
    assert CONFLICT in result.stderr


def test_equivalent_default_tag_forms_still_conflict(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
) -> None:
    """Local parser equivalence must not normalize distinct explicit target strings."""
    repository = "registry.example.invalid/team/package"
    tagged = repository + ":latest"
    assert parse_artifact_ref(repository) == parse_artifact_ref(tagged)
    result = runner.invoke(command, ["-a", repository, "-i", tagged])
    assert result.exit_code == 2 and result.stdout == "" and calls == []
    assert CONFLICT in result.stderr


def test_many_identical_occurrences_invoke_once(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
) -> None:
    """A thousand repeats remain one logical operation and do not leak the tuple to callers."""
    result = runner.invoke(command, ["-i", REFERENCE] * 1000)
    assert result.exit_code == 0 and calls == [REFERENCE]
    assert result.stdout == REFERENCE + "\n" and result.stderr == ""


def test_conflict_after_many_identical_values_is_not_ignored(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
) -> None:
    """The resolver checks the entire occurrence sequence, not just its first two values."""
    result = runner.invoke(command, ["-a", REFERENCE] * 1000 + ["-i", OTHER_REFERENCE])
    assert result.exit_code == 2 and calls == [] and result.stdout == ""


def test_long_reference_is_not_truncated(
    runner: CliRunner, command: click.Command
) -> None:
    """Option handling adds no new parser limits or truncation of caller-owned text."""
    reference = "registry.example.invalid/" + "namespace/" * 500 + "package:latest"
    result = runner.invoke(command, ["-a", reference, "-i", reference])
    assert result.exit_code == 0 and result.stdout == reference + "\n"


@pytest.mark.parametrize(
    "arguments", [[], ["--artifact"], ["-a", REFERENCE, "-i", OTHER_REFERENCE]]
)
def test_resilient_parsing_does_not_enter_command(
    command: click.Command,
    calls: list[str],
    arguments: list[str],
) -> None:
    """Incomplete/conflicting shell-completion contexts do not invent a successful target."""
    with command.make_context("example", arguments, resilient_parsing=True) as context:
        assert context.params["image_ref"] is None
    assert calls == []


def test_normal_empty_callback_input_raises(command: click.Command) -> None:
    """The resolver itself rejects missing input outside Click's resilient mode."""
    with click.Context(command) as context, pytest.raises(click.MissingParameter):
        _resolve_artifact_reference(context, command.params[0], ())


def test_bash_completion_does_not_execute_command(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
) -> None:
    """Click's actual shell-completion entry point remains free of command-body side effects."""
    result = runner.invoke(
        command,
        [],
        prog_name="example",
        env={
            "_EXAMPLE_COMPLETE": "bash_complete",
            "COMP_WORDS": "example --artifact ",
            "COMP_CWORD": "2",
        },
    )
    assert result.exit_code == 0 and result.stderr == "" and calls == []


def test_state_does_not_leak_between_invocations(
    runner: CliRunner,
    command: click.Command,
    calls: list[str],
) -> None:
    """Repeated use of the same Click command resolves only the current invocation's values."""
    first = runner.invoke(command, ["-a", REFERENCE])
    second = runner.invoke(command, ["-i", OTHER_REFERENCE])
    assert first.exit_code == second.exit_code == 0
    assert calls == [REFERENCE, OTHER_REFERENCE]


def test_decorator_preserves_callable_and_legacy_keyword() -> None:
    """The option declaration must not wrap or rename the original Python callback."""

    def callback(image_ref: str) -> str:
        """Return the input to verify direct keyword-based callers remain compatible."""
        return image_ref

    decorated = artifact_option(help_text="Reference.")(callback)
    assert decorated is callback and decorated(image_ref=REFERENCE) == REFERENCE
