#!/usr/bin/bash

. .venv/bin/activate

pyinstaller --clean --onefile hsync_client/cli.py --name hsync
