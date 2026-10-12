#!/usr/bin/env fish
set SCRIPT_DIR (dirname (realpath (status filename)))
cd "$SCRIPT_DIR"
set PYTHON_BIN "$SCRIPT_DIR/.venv/bin/python"
if not test -x "$PYTHON_BIN"; set PYTHON_BIN (command -v python); end
$PYTHON_BIN -c '
import torch
print("PyTorch:", torch.__version__)
print("HIP:", getattr(torch.version, "hip", None))
print("CUDA/ROCm available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("Device:", torch.cuda.get_device_name(0))
'
