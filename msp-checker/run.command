#!/bin/bash
cd "$(dirname "$0")"
python3 price_checker.py
read -p "Press Enter to close..."
