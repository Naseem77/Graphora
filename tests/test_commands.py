from app.review import commands


def test_parse_command_recognizes_known_commands():
    assert commands.parse_command("review").name == "review"
    assert commands.parse_command("/pause").name == "pause"
    assert commands.parse_command("HELP").name == "help"
    assert commands.parse_command("resume now please").argument == "now please"


def test_parse_command_treats_questions_as_freeform():
    parsed = commands.parse_command("what changed in auth?")
    assert parsed.name is None
    assert parsed.argument == "what changed in auth?"


def test_parse_command_handles_empty():
    parsed = commands.parse_command("   ")
    assert parsed.name is None
    assert parsed.argument == ""


def test_help_text_lists_commands():
    text = commands.help_text("graphreview")
    assert "@graphreview review" in text
    assert "@graphreview pause" in text
    assert "@graphreview help" in text
