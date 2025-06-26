#!/bin/bash

# Deactivate the Python virtual environment for Linux/macOS
if type deactivate &>/dev/null; then
    deactivate
else
    echo "No active virtual environment to deactivate."
fi
