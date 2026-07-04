"""Qwen XML tool-call fallback parser (endpoint-drift recovery, 2026-07-04).

A vLLM instance launched without ``--tool-call-parser`` returns
``tool_calls=[]`` and leaves the calls as XML text in ``content`` (verified
live against both qwen endpoints). These tests pin the parser to the exact
observed wire format plus the edge shapes we must survive.
"""
import json

from css.model.client import _strip_tool_call_blocks, parse_qwen_xml_tool_calls

# Verbatim capture from http://10.77.110.162:8888/v1 (2026-07-04).
LIVE_SAMPLE = (
    "<tool_call>\n<function=get_weather>\n<parameter=city>\nParis\n"
    "</parameter>\n</function>\n</tool_call>"
)


def test_live_sample_parses_to_openai_shape():
    calls = parse_qwen_xml_tool_calls(LIVE_SAMPLE)
    assert len(calls) == 1
    c = calls[0]
    assert c["type"] == "function"
    assert c["function"]["name"] == "get_weather"
    assert json.loads(c["function"]["arguments"]) == {"city": "Paris"}


def test_multiple_calls_and_typed_values():
    text = (
        "Let me do both.\n"
        "<tool_call>\n<function=transfer>\n"
        "<parameter=amount>\n42.5\n</parameter>\n"
        "<parameter=fast>\ntrue\n</parameter>\n"
        "<parameter=tags>\n[\"a\", \"b\"]\n</parameter>\n"
        "</function>\n</tool_call>\n"
        "<tool_call>\n<function=fs.ls>\n<parameter=path>\n~/docs\n</parameter>\n"
        "</function>\n</tool_call>"
    )
    calls = parse_qwen_xml_tool_calls(text)
    assert [c["function"]["name"] for c in calls] == ["transfer", "fs.ls"]
    a0 = json.loads(calls[0]["function"]["arguments"])
    assert a0 == {"amount": 42.5, "fast": True, "tags": ["a", "b"]}
    # non-JSON string value survives as string (tilde intact)
    a1 = json.loads(calls[1]["function"]["arguments"])
    assert a1 == {"path": "~/docs"}
    # distinct synthetic ids
    assert len({c["id"] for c in calls}) == 2


def test_no_tool_call_returns_empty():
    assert parse_qwen_xml_tool_calls("Plain final answer, no tools.") == []
    assert parse_qwen_xml_tool_calls("") == []


def test_zero_arg_call_and_missing_close_tag():
    # Some emissions omit </function> before </tool_call>.
    text = "<tool_call>\n<function=show_current_time>\n</tool_call>"
    calls = parse_qwen_xml_tool_calls(text)
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "show_current_time"
    assert json.loads(calls[0]["function"]["arguments"]) == {}


def test_strip_keeps_surrounding_prose():
    text = "I will check the weather.\n" + LIVE_SAMPLE + "\nDone."
    assert _strip_tool_call_blocks(text) == "I will check the weather.\n\nDone."
