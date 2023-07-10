"""
Entry point for running hsync_client as a module.

Usage:
    python -m hsync_client [options]

This module allows the package to be run directly using Python's -m flag.
"""

from hsync_client.main import main


if __name__ == "__main__":
    main()

