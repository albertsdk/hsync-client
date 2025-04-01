#!/usr/bin/bash

. .venv/bin/activate

pyinstaller --clean --onefile src/hsync.py --name hsync
