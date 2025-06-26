#!/bin/bash

# Activate the Python virtual environment for Linux/macOS
if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
else
    echo "No virtual environment found at .venv/bin/activate"
    exit 1
fi
