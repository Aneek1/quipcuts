"""Unit tests for the resilient LLM-output parser.  Run:  python -m tests.test_parser"""

from openclip.analyzer import extract_json_payload, parse_clip_suggestions, resolve_llm

GOOD = '{"start_time": 10, "end_time": 50, "headline_title": "Hook", "virality_score_rationale": "r", "virality_score": 80}'

CASES = {
    "plain": '{"clips": [%s]}' % GOOD,
    "prose + fence": "Sure! Here are the clips:\n```json\n{\"clips\": [%s]}\n```\nHope this helps." % GOOD,
    "think block": "<think>the transcript says {not json}</think>{\"clips\": [%s]}" % GOOD,
    "trailing commas": '{"clips": [{"start_time": 10, "end_time": 50, "headline_title": "Hook",'
                       ' "virality_score_rationale": "r", "virality_score": 80,},],}',
    "bare array": "[%s]" % GOOD,
    "smart quotes": "{“clips”: [%s]}" % GOOD,
    "python dict": "{'clips': [{'start_time': 10, 'end_time': 50, 'headline_title': 'Hook',"
                   " 'virality_score_rationale': 'r', 'virality_score': 80, 'final': True}]}",
    "unquoted keys": "{clips: [{start_time: 10, end_time: 50, headline_title: \"Hook\","
                     " virality_score_rationale: \"r\", virality_score: 80}]}",
    "aliases + mm:ss + /10": '{"highlights": [{"start": "0:10", "end": "0:50", "title": "Hook",'
                             ' "reason": "r", "score": "8/10"}]}',
    "truncated": '{"clips": [%s, {"start_time": 100, "end_time": 1' % GOOD,
    "brackets inside strings": '{"clips": [{"start_time": 10, "end_time": 50, "headline_title": "Why [this] {works}, note: it",'
                               ' "virality_score_rationale": "r, ]", "virality_score": 80,}]}',
}


def main() -> None:
    for name, text in CASES.items():
        clips, errors = parse_clip_suggestions(text)
        assert len(clips) == 1, f"{name}: {clips} {errors}"
        c = clips[0]
        assert (c.start_time, c.end_time, c.virality_score) == (10, 50, 80), f"{name}: {c}"
        print(f"ok  {name:<26} -> {c.headline_title!r}")

    # one bad clip must not sink the good one
    clips, errors = parse_clip_suggestions('{"clips": [%s, {"start_time": 60, "end_time": 20}]}' % GOOD)
    assert len(clips) == 1 and errors, errors
    print("ok  invalid clip skipped       ->", errors[0])
    assert extract_json_payload('x {"a": "}"} y') == {"a": "}"}

    assert resolve_llm("auto", "", "ollama/qwen2.5") == ("ollama", "qwen2.5")
    assert resolve_llm("ollama", "sk-x", "anthropic/claude-sonnet-5-5", "llama3.2") == ("ollama", "llama3.2")
    assert resolve_llm("auto", "sk-x", "anthropic/claude-sonnet-5-5")[0] == "litellm"
    print("PARSER TESTS PASSED")


if __name__ == "__main__":
    main()
