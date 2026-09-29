"""Windows desktop entry point; freeze_support must precede Qt imports."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from nsa_app.gui import main
    raise SystemExit(main())
