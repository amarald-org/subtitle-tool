"""Entry point for the standalone app.

Opens the editor window. `Subtitle Tool --cli ...` runs the command-line tool
instead, e.g. `Subtitle Tool --cli movie.mkv --auto -t`.
"""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    if sys.argv[1:2] == ["--cli"]:
        del sys.argv[1]
        from subtitle_tool import main

        main()
    else:
        from subtitle_tool.gui import main

        main(sys.argv[1:])
