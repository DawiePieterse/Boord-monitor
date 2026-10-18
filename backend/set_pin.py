"""Set or reset the admin PIN, from the server console.

    C:\\boord-monitor\\backend\\.venv\\Scripts\\python.exe C:\\boord-monitor\\backend\\set_pin.py

Deliberately not possible from the web page - see admin_auth.py. Setting a
new PIN signs every phone out of the dashboard.
"""
import getpass
import sys

import admin_auth


def ask(prompt: str) -> str:
    """Hidden typing where the console allows it. Windows' getpass reads the
    keyboard directly, and over AnyDesk it receives what looks like Ctrl+C and
    raises KeyboardInterrupt before anyone types - which is how v1.0's
    installer skipped the PIN on the farm server. Fall back to a visible
    prompt then; a real Ctrl+C at that prompt still aborts."""
    try:
        return getpass.getpass(prompt)
    except KeyboardInterrupt:
        print("\nThis console cannot hide typing (common over remote desktop) - "
              "the PIN will show as you type.")
        return input(prompt)


def main() -> None:
    prompt = f"New admin PIN (at least {admin_auth.MIN_PIN_LENGTH} characters): "
    pin = ask(prompt)
    if ask("Type it again: ") != pin:
        sys.exit("The two PINs differ - nothing changed.")
    try:
        admin_auth.set_pin(pin)
    except ValueError as e:
        sys.exit(f"{e} - nothing changed.")
    print("Admin PIN set. Every phone will need to sign in again before restarting or changing alerts.")


if __name__ == "__main__":
    main()
