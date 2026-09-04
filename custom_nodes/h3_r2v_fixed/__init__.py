"""Fixed-socket MiniMax H3 Ref2V node for reliable API submission.

ComfyUI's native Ref2V node uses Autogrow dictionaries.  Some releases accept
those dictionaries through POST /prompt but silently drop their contents.  A
small set of ordinary optional IMAGE sockets avoids that serialization path.
The encoding below intentionally mirrors the image portion of ComfyUI's native
MiniMaxH3ReferenceToVideo implementation.
"""

from __future__ import annotations

import math

import node_helpers
from comfy_extras.nodes_minimax_h3 import (
    CANVAS_MULTIPLE,
    REF_IMAGE_SHORT_EDGE,
    _empty_av_latent,
    _resize,
)


class H3ReferenceToVideoFixed:
    @classmethod
    def INPUT_TYPES(cls):
        optional = {f"ref_image_{i}": ("IMAGE",) for i in range(1, 10)}
        return {
            "required": {
                "clip": ("CLIP",),
                "vae": ("VAE",),
                "audio_vae": ("VAE",),
                "prompt": ("STRING", {"multiline": True, "dynamicPrompts": True}),
                "width": ("INT", {"default": 448, "min": 32, "max": 16384, "step": 32}),
                "height": ("INT", {"default": 448, "min": 32, "max": 16384, "step": 32}),
                "length": ("INT", {"default": 124, "min": 5, "max": 3600, "step": 17}),
                "ref_image_size": (["match", "max"], {"default": "match"}),
            },
            "optional": optional,
        }

    RETURN_TYPES = ("CONDITIONING", "LATENT")
    RETURN_NAMES = ("positive", "latent")
    FUNCTION = "encode"
    CATEGORY = "model/conditioning/minimax"
    DESCRIPTION = "MiniMax H3 Ref2V with nine fixed image sockets for API workflows."

    def encode(self, clip, vae, audio_vae, prompt, width, height, length,
               ref_image_size="match", **kwargs):
        del audio_vae  # retained for graph compatibility with the native node
        latent, _frame_count = _empty_av_latent(width, height, length)
        ref_items = []
        ref_blocks = []

        for index in range(1, 10):
            img = kwargs.get(f"ref_image_{index}")
            if img is None:
                continue
            h, w = img.shape[1], img.shape[2]
            if ref_image_size == "match":
                scale = min(1.0, math.sqrt((width * height) / (w * h)))
            else:
                scale = min(1.0, REF_IMAGE_SHORT_EDGE / min(w, h))
            tw = max(CANVAS_MULTIPLE,
                     round(w * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            th = max(CANVAS_MULTIPLE,
                     round(h * scale / CANVAS_MULTIPLE) * CANVAS_MULTIPLE)
            resized = _resize(img[:1], tw, th, "disabled")
            z = vae.encode(resized)
            ref_items.append({"type": "image", "data": resized})
            ref_blocks.append({
                "kind": "image",
                "latent_h": th // 16,
                "latent_w": tw // 16,
                "latent": z,
            })

        tokens = clip.tokenize(prompt, minimax_ref_items=ref_items)
        cond = clip.encode_from_tokens_scheduled(tokens)
        if ref_blocks:
            cond = node_helpers.conditioning_set_values(
                cond, {"minimax_refs": ref_blocks})
        return cond, latent


NODE_CLASS_MAPPINGS = {
    "H3ReferenceToVideoFixed": H3ReferenceToVideoFixed,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "H3ReferenceToVideoFixed": "MiniMax H3 Ref2V (Fixed API Images)",
}

