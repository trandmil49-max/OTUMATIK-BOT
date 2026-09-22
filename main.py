"""
main.py

Thin top-level wrapper so the platform can be started the conventional
way (`python main.py`, or a Railway/Docker `CMD python main.py`) without
requiring `python -m application.main`. All real logic lives in
application/main.py; this file exists purely for invocation convenience.
"""

from application.main import main

if __name__ == "__main__":
    main()
