"""Runtime patches for speech-to-speech.

Injected over PYTHONPATH inside its own venv, leaving the upstream checkout alone::

    PYTHONPATH=/path/to/BiliSama/tools/s2s_shim \
      /path/to/s2s-venv/bin/python -m bilisama_s2s_shim serve config.json
"""

from bilisama_s2s_shim.patches import PatchError, PatchResult, apply_patches

__all__ = ["PatchError", "PatchResult", "apply_patches"]
