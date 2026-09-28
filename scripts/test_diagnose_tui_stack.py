"""Check that a stale diagnostic fixture fails closed and assertions survive."""

from diagnose_tui_stack import END, MARKER, STAGES, START, TUI_READY, instrument_fixture


def main() -> None:
    statements = [statement for label, statement in STAGES for _ in range(2 if label == "retry" else 1)]
    fixture = (
        "// preceding test is untouched\n" + START + TUI_READY + "\n"
        + "\n".join(statements) + '\n    assert_eq!(draft, "Retained task draft");\n    Ok(())\n}\n'
        + END + " -> Result<()> { Ok(()) }\n"
    )
    result = instrument_fixture(fixture)
    assert result.startswith("// preceding test is untouched\n")
    assert result[result.index(END):] == fixture[fixture.index(END):]
    for statement in statements + ['assert_eq!(draft, "Retained task draft");']:
        assert result.count(statement) == fixture.count(statement), statement
    assert "before-retry-1" in result and "after-retry-2" in result
    assert "scenario_future_bytes" in result and "resume_future_bytes" in result
    assert result.count(START) == 1
    for stale in (result, fixture.replace(TUI_READY, ""), fixture.replace(STAGES[0][1], ""), fixture + START):
        try:
            instrument_fixture(stale)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe fixture was accepted")
    assert MARKER not in fixture
    print("TUI stack diagnostic instrumentation: pass")


if __name__ == "__main__":
    main()
