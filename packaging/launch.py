from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    import sys
    from pathlib import Path
    if sys.argv[1:] == ["--quota-statusline"]:
        from mixharness.quota_statusline import capture_statusline
        capture_statusline()
        raise SystemExit(0)
    if Path(sys.executable).stem.casefold() == "mixharness-cli" and sys.argv[1:2] not in (["--check-worker"], ["--standard-check"]):
        from mixharness.cli import main
    else:
        from mixharness.desktop_main import main
    raise SystemExit(main())
