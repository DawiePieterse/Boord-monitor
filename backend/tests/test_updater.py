"""The updater is a batch file nobody can run off the farm PC, so the
properties a past failure taught us are pinned here as text checks."""


def test_updater_starts_the_new_file_after_the_checkout():
    """cmd reads a batch file off the disk as it runs, so once the checkout has
    replaced the updater it must start the new file from the top, as the last
    thing in that block: anything after it would be read at a stale byte of
    the new file (Kudde v0.12 ended "failed" that way, the server never
    restarted)."""
    import os
    import re
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "update_monitor_server.bat"), encoding="utf-8") as fh:
        text = fh.read()
    assert re.search(r'git checkout --force "!NEWTAG!"\n.*?\n    "%~f0" %\* --checked-out\n\)\n', text, re.S)
    # ...and a second time round stops, rather than checking out for ever.
    assert '"%%~A"=="--checked-out"' in text
    # pip through the venv's python: Scripts\pip.exe is blocked on the farm PC.
    assert not re.search(r"^pip install", text, re.M)
