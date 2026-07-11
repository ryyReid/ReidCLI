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
