"""speech-to-speech 的运行时补丁。

在它自己的 venv 里通过 PYTHONPATH 注入，上游检出目录保持干净：

    PYTHONPATH=/path/to/BiliSama/tools/s2s_shim \
      /path/to/s2s-venv/bin/python -m bilisama_s2s_shim serve config.json
"""

from bilisama_s2s_shim.patches import PatchError, PatchResult, apply_patches

__all__ = ["PatchError", "PatchResult", "apply_patches"]
