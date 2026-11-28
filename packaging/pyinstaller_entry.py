"""PyInstaller entrypoint located outside src/moodle_cli/ so package modules
such as moodle_cli.calendar never shadow Python standard library modules."""

from moodle_cli.cli import main

if __name__ == "__main__":
    main()
