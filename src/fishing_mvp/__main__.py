if __package__:
    from .cli import main
else:  # PyInstaller executes the analysis entrypoint as a top-level script.
    from fishing_mvp.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
