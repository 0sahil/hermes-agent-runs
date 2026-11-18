"""Reading a Claude Code transcript: event parsing, tail-from-end and byte cursors."""
import json

from adapters.claude import forward_events, line_events, tail_events
from conftest import SID_A, assistant, tool_result, tool_use, ts, user_prompt

# --- one line → events -------------------------------------------------------------------------


def test_prompt_is_cut_to_about_2000_chars():
    [ev] = line_events(user_prompt("x" * 5000))
    assert ev["kind"] == "prompt" and ev["title"] == "Prompt"
    assert 1990 <= len(ev["detail"]) <= 2000 and ev["detail"].endswith("…")


def test_assistant_text_is_kept_in_full():
    text = "First line of the answer.\n" + "more " * 2000
    [ev] = line_events(assistant([{"type": "text", "text": text}]))
    assert ev["kind"] == "text" and ev["title"] == "First line of the answer."
    assert ev["detail"] == text.strip()


def test_short_text_has_no_redundant_detail():
    [ev] = line_events(assistant([{"type": "text", "text": "Done."}]))
    assert ev == {"ts": ev["ts"], "kind": "text", "title": "Done."}


def test_thinking_is_hidden_unless_asked_for():
    line = assistant([{"type": "thinking", "thinking": "an invented thought", "signature": "s"}])
    assert line_events(line) == []
    [ev] = line_events(line, show_thinking=True)
    assert ev["kind"] == "thinking" and ev["detail"] == "an invented thought"
    assert line_events(assistant([{"type": "thinking", "thinking": "", "signature": "s"}]), True) == []


def test_tool_details():
    cases = [
        ("Bash", {"command": "npm test -- --watch=false", "description": "run tests"}, "npm test -- --watch=false"),
        ("Read", {"file_path": "/work/demo-app/src/app.ts", "limit": 40}, "/work/demo-app/src/app.ts"),
        ("Edit", {"file_path": "/work/demo-app/README.md", "old_string": "a", "new_string": "b"}, "/work/demo-app/README.md"),
        ("Write", {"file_path": "/work/demo-app/new.txt", "content": "long"}, "/work/demo-app/new.txt"),
        ("Grep", {"pattern": "TODO", "path": "src"}, "TODO  in src"),
        ("Glob", {"pattern": "**/*.py"}, "**/*.py"),
        ("WebFetch", {"url": "https://example.com/docs", "prompt": "summarize"}, "https://example.com/docs"),
        ("Task", {"description": "Explore the parser", "prompt": "long"}, "Explore the parser"),
        ("Agent", {"description": "Review the diff", "prompt": "long"}, "Review the diff"),
        ("TodoWrite", {"todos": [{"content": "a"}, {"content": "b"}, {"content": "c"}]}, "3 todos"),
        ("TodoWrite", {"todos": [{"content": "a"}]}, "1 todo"),
    ]
    for name, inp, want in cases:
        [ev] = line_events(tool_use(name, inp))
        assert (ev["kind"], ev["title"], ev["detail"]) == ("tool", name, want), name


def test_unknown_tool_input_is_compact_json_cut_to_300():
    [ev] = line_events(tool_use("mcp__demo__lookup", {"query": "q" * 1000, "n": 3}))
    assert ev["title"] == "mcp__demo__lookup"
    assert ev["detail"].startswith('{"query":"qqq') and len(ev["detail"]) <= 300


def test_tool_results_and_errors():
    [ok] = line_events(tool_result("line one\n" + "y" * 1000))
    assert ok["kind"] == "result" and ok["title"] == "Result" and len(ok["detail"]) <= 400
    [bad] = line_events(tool_result("command not found", is_error=True))
    assert (bad["kind"], bad["title"], bad["detail"]) == ("error", "Error", "command not found")
    [blocks] = line_events(tool_result([{"type": "text", "text": "from blocks"}, {"type": "image", "source": {}}]))
    assert blocks["detail"] == "from blocks\n[image]"


def test_system_events():
    [interrupted] = line_events(user_prompt("[Request interrupted by user for tool use]"))
    assert (interrupted["kind"], interrupted["title"]) == ("system", "Interrupted by the user")
    [compact] = line_events({"type": "user", "isCompactSummary": True, "message": {"content": "summary..."}})
    assert compact["title"] == "Conversation compacted"
    [boundary] = line_events({"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted"})
    assert boundary["title"] == "Conversation compacted"
    [api] = line_events({"type": "system", "subtype": "api_error", "level": "error", "timestamp": ts(),
                         "error": {"status": 529, "message": "Overloaded"}, "retryAttempt": 2, "maxRetries": 10})
    assert (api["kind"], api["title"], api["detail"]) == ("system", "API error 529, retrying (2/10)", "Overloaded")
    [cmd] = line_events(user_prompt("<command-name>/compact</command-name>\n<command-message>compact</command-message>"))
    assert cmd["title"] == "Command /compact"
    assert line_events(user_prompt("<local-command-stdout>ok</local-command-stdout>")) == []


def test_api_error_message_from_assistant_is_an_error():
    line = assistant([{"type": "text", "text": "Invalid API key"}], model="<synthetic>")
    line["isApiErrorMessage"] = True
    [ev] = line_events(line)
    assert (ev["kind"], ev["title"], ev["detail"]) == ("error", "API error", "Invalid API key")


def test_unknown_and_meta_lines_give_no_events():
    for line in [{"type": "attachment", "attachment": {}}, {"type": "api-request-blob", "message": {"content": [
                 {"type": "text", "text": "request copy"}]}}, {"type": "queue-operation", "content": "queued"},
                 {"type": "brand-new-type", "x": 1}, {"type": "user", "isMeta": True, "message": {"content": "Caveat"}},
                 {"type": "assistant", "message": {"content": [{"type": "new_block_kind", "x": 1}]}},
                 {"type": "assistant", "message": "not a dict"}, {"no": "type"}]:
        assert line_events(line) == [], line


def test_timestamps_become_epoch_ms():
    [ev] = line_events(user_prompt("hi", t=1.5))
    assert ev["ts"] == 1_700_000_001_500


# --- files -------------------------------------------------------------------------------------


def write(path, lines):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write((line if isinstance(line, str) else json.dumps(line)) + "\n")


def test_malformed_and_partial_lines_do_not_break_reading(tmp_path):
    path = tmp_path / f"{SID_A}.jsonl"
    write(path, [user_prompt("the prompt"), "{not json", '{"type":"assistant","message":', "",
                 assistant([{"type": "text", "text": "answer"}])])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"type":"assistant","message":{"content":[{"type":"text","text":"half wri')  # being written
    got = tail_events(path, 50)
    assert [e["kind"] for e in got["events"]] == ["prompt", "text"]
    assert got["cursor"] < path.stat().st_size  # the half-written line is not consumed


def test_tail_reads_only_the_end_of_a_large_file(tmp_path, monkeypatch):
    path = tmp_path / f"{SID_A}.jsonl"
    lines = [user_prompt("the very first prompt")]
    filler = {"type": "api-request-blob", "message": {"content": [{"type": "text", "text": "z" * 4000}]}}
    for i in range(3000):
        lines.append(filler)
        lines.append(tool_use("Bash", {"command": f"echo {i}"}, tid=f"t{i}"))
    write(path, lines)
    assert path.stat().st_size > 10_000_000

    import adapters.claude as claude
    reads = []
    real = claude._events_of

    def counting(chunk_lines, show):
        chunk_lines = list(chunk_lines)
        reads.append(sum(len(x) for x in chunk_lines))
        return real(chunk_lines, show)

    monkeypatch.setattr(claude, "_events_of", counting)
    got = tail_events(path, 300)
    events = got["events"]
    assert len(events) == 300
    # The run's first prompt is shown first even though it is megabytes back.
    assert events[0]["kind"] == "prompt" and events[0]["detail"] == "the very first prompt"
    assert events[-1]["detail"] == "echo 2999"
    assert events[1]["detail"] == "echo 2701"
    assert sum(reads) < 3_000_000  # read well under a third of the file (plus the head for the prompt)
    assert got["cursor"] == path.stat().st_size


def test_cursor_returns_only_appended_events_and_handles_a_partial_line(tmp_path):
    path = tmp_path / f"{SID_A}.jsonl"
    write(path, [user_prompt("go"), tool_use("Bash", {"command": "ls"})])
    first = tail_events(path, 300)
    assert len(first["events"]) == 2
    cursor = first["cursor"]

    nothing = forward_events(path, cursor, 300)
    assert nothing["events"] == [] and nothing["cursor"] == cursor and not nothing["reset"]

    line = json.dumps(tool_result("file-a\nfile-b"))
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line[:30])  # half a line
    partial = forward_events(path, cursor, 300)
    assert partial["events"] == [] and partial["cursor"] == cursor

    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line[30:] + "\n" + json.dumps(assistant([{"type": "text", "text": "Found two files."}])) + "\n")
    more = forward_events(path, cursor, 300)
    assert [e["kind"] for e in more["events"]] == ["result", "text"]
    assert more["cursor"] == path.stat().st_size


def test_cursor_respects_the_limit_and_continues(tmp_path):
    path = tmp_path / f"{SID_A}.jsonl"
    write(path, [tool_use("Bash", {"command": f"echo {i}"}) for i in range(10)])
    got = forward_events(path, 0, 4)
    assert [e["detail"] for e in got["events"]] == ["echo 0", "echo 1", "echo 2", "echo 3"] and got["more"]
    rest = forward_events(path, got["cursor"], 100)
    assert [e["detail"] for e in rest["events"]] == [f"echo {i}" for i in range(4, 10)] and not rest["more"]


def test_file_that_shrank_or_was_replaced_restarts_from_the_end(tmp_path):
    path = tmp_path / f"{SID_A}.jsonl"
    write(path, [tool_use("Bash", {"command": f"echo {i}"}) for i in range(20)])
    cursor = tail_events(path, 300)["cursor"]
    write(path, [user_prompt("a new file"), tool_use("Read", {"file_path": "/work/x"})])
    got = forward_events(path, cursor, 300)
    assert got["reset"] is True
    assert [e["kind"] for e in got["events"]] == ["prompt", "tool"]
    assert got["cursor"] == path.stat().st_size


def test_cursor_not_on_a_line_boundary_restarts(tmp_path):
    path = tmp_path / f"{SID_A}.jsonl"
    write(path, [user_prompt("one"), user_prompt("two")])
    got = forward_events(path, 5, 300)
    assert got["reset"] is True and len(got["events"]) == 2
