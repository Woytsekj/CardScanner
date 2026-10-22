# OCR GPU acceleration notes

The desktop scanner uses EasyOCR, which delegates inference to PyTorch. GPU support depends on the exact PyTorch wheel/runtime and driver combination; the presence of a GPU alone does not mean OCR is using it.

## Check the active Python environment

Run from `scanner-client/` after installing dependencies:

```fish
./check_ocr_gpu.fish
```

The output should include the PyTorch version, `HIP` runtime (for AMD ROCm builds), CUDA/ROCm availability, and device name if available. Run the command using the same virtual environment that launches `Scanner.py`.

## Choose the OCR mode

In `scanner.env.fish`:

```fish
# Automatically use a supported PyTorch CUDA/ROCm device, otherwise CPU.
set -gx MTG_OCR_GPU "auto"

# To avoid GPU use while gaming or diagnosing instability:
# set -gx MTG_OCR_GPU "cpu"
```

Supported values are `auto`, `cpu`/`0`, and `gpu`/`1`. If GPU reader initialization fails, the current scanner falls back to CPU mode. Check the terminal output at startup to confirm which device mode was selected.

## Installation caution

Use a PyTorch build that matches your OS, Python version, architecture, and GPU runtime. Do not copy CUDA-only install instructions onto an AMD ROCm system or mix wheels from unrelated environments. On Linux, ROCm versions and hardware support are version-specific; consult the official PyTorch/ROCm installation guidance for your installed stack and rerun the check script after changes.

If OCR performance is acceptable on CPU, force CPU mode to keep the scanner from competing with games or other GPU workloads. Recognition quality is not improved merely by enabling GPU acceleration; it only changes inference execution/performance.
