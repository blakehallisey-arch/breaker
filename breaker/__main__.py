"""`python3 -m breaker` is the same entry point as running breaker/hook.py.

Two ways in on purpose: the module form is the nice one to type, and the bare
script path is what a hook config wants when the package is not on sys.path.
"""
from .hook import main

if __name__ == "__main__":
    main()
