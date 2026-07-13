"""Tests for prompt-based chain-of-thought splitting."""
from reidx.runtime.reasoning import split_reasoning


def test_splits_think_block():
    thinking, answer = split_reasoning("<think>let me reason</think>The answer is 42.")
    assert thinking == "let me reason"
    assert answer == "The answer is 42."


def test_handles_thinking_tag_variant_and_whitespace():
    thinking, answer = split_reasoning("  <thinking>\n step one\n</thinking>\n\n Done. ")
    assert thinking == "step one"
    assert answer == "Done."


def test_no_tags_returns_full_answer():
    thinking, answer = split_reasoning("Just a plain answer.")
    assert thinking is None
    assert answer == "Just a plain answer."


def test_malformed_unclosed_tag_never_hides_answer():
    # No closing tag -> treat everything as the answer, don't drop content.
    text = "<think>partial reasoning that never closes and the answer"
    thinking, answer = split_reasoning(text)
    assert thinking is None
    assert answer == text


def test_preamble_before_think_is_kept_in_answer():
    thinking, answer = split_reasoning("Sure!<think>reasoning</think> Here you go.")
    assert thinking == "reasoning"
    assert answer == "Sure! Here you go."


def test_empty_input():
    assert split_reasoning("") == (None, "")


def test_fenced_think_block():
    text = "```thinking\nNeed a plan.\n```\n\nHere is the fix."
    thinking, answer = split_reasoning(text)
    assert thinking == "Need a plan."
    assert answer == "Here is the fix."


def test_untagged_monologue_goes_to_thinking():
    """GLM-style: meta first line, blank line, then the real user-facing reply."""
    text = (
        "User just said hello. No tool use needed.\n"
        "\n"
        "Hello! How can I help you today? I'm ready to assist with coding."
    )
    thinking, answer = split_reasoning(text)
    assert thinking == "User just said hello. No tool use needed."
    assert answer.startswith("Hello!")
    assert "tool use" not in answer


def test_plain_two_paragraph_answer_not_stolen():
    """Normal multi-paragraph answers must not be misclassified as thinking."""
    text = (
        "Here is the overview of the module.\n"
        "\n"
        "It handles config loading and validation for the CLI."
    )
    thinking, answer = split_reasoning(text)
    assert thinking is None
    assert answer == text


def test_parameter_name_reasoning_tag():
    """GLM / tool-style: <parameter name=\"reasoning\">…</parameter>."""
    text = (
        '<parameter name="reasoning">User just said hello. No tool use needed.</parameter>\n'
        "\n"
        "Hello! How can I help you today? I'm ready to assist with coding, "
        "file operations, or any task in this workspace."
    )
    thinking, answer = split_reasoning(text)
    assert thinking == "User just said hello. No tool use needed."
    assert answer.startswith("Hello!")
    assert "parameter" not in answer
    assert "tool use" not in answer


def test_parameter_name_reasoning_with_bom():
    text = (
        '\ufeff<parameter name="reasoning">Quick plan.</parameter>\n'
        "Done."
    )
    thinking, answer = split_reasoning(text)
    assert thinking == "Quick plan."
    assert answer == "Done."
