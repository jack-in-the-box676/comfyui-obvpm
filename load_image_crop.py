"""Load Image & Crop: LoadImage plus an interactive crop rectangle.

The frontend (web/load_image_crop.js) shows the picked image on the node
and lets the user drag/resize a crop area. The selection is stored in the
hidden "crop" string widget as JSON with normalized coordinates
{"x":0..1,"y":0..1,"w":0..1,"h":0..1}. Empty string means no crop.
"""

import json
import os
import math

import numpy as np
import torch
from PIL import ImageOps

import folder_paths
import node_helpers

from .image_safety import (image_path, inspect_images, hash_images, bounded_text,
                           finite_number, check_pixels, open_checked_image)


def _input_images():
    """Every file under the input directory, as input-relative paths.

    Core's LoadImage lists the input folder FLAT (os.listdir), so images
    filed in subfolders are invisible to it -- and a folder per project
    is the obvious way to keep a few hundred references straight. This
    walks instead, and returns paths in the form the rest of the stack
    already understands: relative to the input dir, forward slashes,
    e.g. "selfie_walk2/ref_01.png". That is the same shape core's own
    Load3D node produces (comfy_extras/nodes_load_3d.py), it is what
    our pack-owned path resolver confines to the selected image directory, and
    the crop editor's front end already splits it into ?subfolder= and
    ?filename= for its preview.

    Symlinked directories are NOT followed: os.walk's default. A loop
    there would hang the node list, and the node menu is built often.
    """
    root = folder_paths.get_input_directory()
    out = []
    for dirpath, _subdirs, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            try:
                image_path(os.path.relpath(os.path.join(dirpath, name), root))
            except (ValueError, OSError):
                continue
            if rel_dir == os.curdir:
                out.append(name)
            else:
                out.append(os.path.join(rel_dir, name).replace(os.sep, "/"))
    return sorted(out)


def _parse_crop(crop, width, height):
    """Return (x0, y0, x1, y1) pixel box, or None for full image."""
    if not crop:
        return None
    bounded_text(crop, 4096, "Crop JSON")
    try:
        data = json.loads(crop)
        x = float(data["x"])
        y = float(data["y"])
        w = float(data["w"])
        h = float(data["h"])
    except (ValueError, KeyError, TypeError):
        return None
    if not all(math.isfinite(v) and 0 <= v <= 1 for v in (x, y, w, h)):
        raise ValueError("Crop coordinates must be finite normalized numbers between 0 and 1")
    # Round size independently from position. This keeps the actual crop
    # dimensions stable when the same rectangle is moved across the image.
    crop_w = max(1, min(width, round(w * width)))
    crop_h = max(1, min(height, round(h * height)))
    x0 = max(0, min(width - crop_w, round(x * width)))
    y0 = max(0, min(height - crop_h, round(y * height)))
    x1 = x0 + crop_w
    y1 = y0 + crop_h
    if x0 == 0 and y0 == 0 and x1 == width and y1 == height:
        return None
    return (x0, y0, x1, y1)


# Named presets. Core Resolution Selector labels are retained where
# applicable; OBVPM's additional ratios are named consistently.
ASPECT_RATIOS = {
    "Free": None,
    "1:1 (Square)": (1, 1),
    "2:3 (Portrait Photo)": (2, 3),
    "3:2 (Photo)": (3, 2),
    "3:4 (Portrait Standard)": (3, 4),
    "4:3 (Standard)": (4, 3),
    "9:16 (Portrait Widescreen)": (9, 16),
    "16:9 (Widescreen)": (16, 9),
    "21:9 (Ultrawide)": (21, 9),
    "1:2 (Portrait 2:1)": (1, 2),
    "2:1 (Wide 2:1)": (2, 1),
    "4:5 (Portrait 5:4)": (4, 5),
    "5:4 (Landscape 5:4)": (5, 4),
}
ASPECTS = tuple(ASPECT_RATIOS)


def _aspect_value(aspect):
    """Named preset -> finite positive width/height; None for Free."""
    if aspect in (None, "", "free", "Free"):
        return None
    pair = ASPECT_RATIOS.get(aspect)
    if pair is None:
        raise ValueError("Invalid aspect ratio: %r" % aspect)
    return pair[0] / pair[1]



def _target_dimensions(megapixels, ratio, multiple, aspect_pair=None):
    """Resolution Selector-style target: ideal aspect/MP, then round W/H independently."""
    total_pixels = megapixels * 1024 * 1024
    ideal_height = math.sqrt(total_pixels / ratio)
    ideal_width = ideal_height * ratio
    width = round(ideal_width / multiple) * multiple
    height = round(ideal_height / multiple) * multiple
    return max(multiple, width), max(multiple, height)

def _centered_box(width, height, ratio):
    """The largest centered (x0, y0, x1, y1) of pixel ratio `ratio`.

    What an empty crop MEANS under a fixed aspect: the full image is not
    that shape, so the honest default is the biggest cut of it that is.
    The editor shows the same rectangle (implied, dashed) so what is on
    the node is what runs.
    """
    w = width
    h = round(w / ratio)
    if h > height:
        h = height
        w = round(h * ratio)
    x0 = (width - w) // 2
    y0 = (height - h) // 2
    return (x0, y0, x0 + max(1, w), y0 + max(1, h))


class LoadImageCrop:
    CATEGORY = "obvpm/image"
    FUNCTION = "load"
    RETURN_TYPES = ("IMAGE", "MASK", "INT", "INT")
    RETURN_NAMES = ("image", "mask", "width", "height")
    DESCRIPTION = (
        "Loads an image and crops it to the area selected interactively on "
        "the node's preview. Drag to draw the crop area, drag inside it to "
        "move, drag its corners to resize, click to clear. With no crop "
        "drawn the full image is output. The selected area is resized using "
        "Resolution Selector-style megapixel and multiple controls."
    )

    OUTPUT_TOOLTIPS = (
        "The loaded image, cropped to the selection and resized to the requested resolution.",
        "Mask from the image's alpha channel, cropped and resized the same way.",
        "Final output width in pixels.",
        "Final output height in pixels.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        files = folder_paths.filter_files_content_types(
            _input_images(), ["image"])
        return {
            "required": {
                "image": (sorted(files), {
                    "image_upload": True,
                    "tooltip": "The image file to load. Upload, drag & drop, or pick an existing input file.",
                }),
                "crop": ("STRING", {
                    "default": "",
                    "tooltip": "Managed by the crop editor on the node — no need to edit by hand.",
                }),
                "aspect": (list(ASPECTS), {
                    "default": "Free",
                    "tooltip": "Pin the crop rectangle to a fixed aspect "
                               "ratio: drawing and resizing keep the "
                               "shape, and with no crop drawn the output "
                               "is the largest centered cut of that "
                               "ratio. 'Free' is the unconstrained "
                               "editor. A stored crop that disagrees "
                               "with the ratio refuses at run time "
                               "rather than being silently reshaped.",
                }),
                "use_megapixels": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Enable target megapixel resizing.",
                }),
                "megapixels": ("FLOAT", {
                    "default": 1.0, "min": 0.01, "max": 128.0, "step": 0.01,
                    "tooltip": "Target output size in megapixels (1 MP = 1024 x 1024 pixels).",
                }),
                "use_multiple": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Enable multiple-aligned crop sizes and output dimensions.",
                }),
                "multiple": ("INT", {
                    "default": 8, "min": 1, "max": 1024, "step": 1,
                    "tooltip": "Crop/output width and height are constrained to this pixel multiple.",
                }),
            }
        }

    def load(self, image, crop="", aspect="Free", use_megapixels=True,
             megapixels=1.0, use_multiple=True, multiple=8):
        megapixels = finite_number(megapixels, "megapixels", 0.01, 128)
        multiple = int(finite_number(multiple, "multiple", 1, 1024))
        _aspect_value(aspect)
        _parse_crop(crop, 1, 1)
        paths, counts = inspect_images([image], animation=True)
        output_images, output_masks = [], []
        size = None
        total = 0
        with open_checked_image(paths[0]) as img:
            for index in range(counts[0]):
                img.seek(index)
                total = check_pixels(*img.size, total)
                i = node_helpers.pillow(ImageOps.exif_transpose, img)
                if size is None:
                    size = i.size
                if i.size != size:
                    continue
                frame = torch.from_numpy(np.array(i.convert("RGB")).astype(np.float32) / 255.0)[None,]
                if "A" in i.getbands():
                    mask = 1.0 - torch.from_numpy(np.array(i.getchannel("A")).astype(np.float32) / 255.0)
                else:
                    mask = torch.zeros((i.height, i.width), dtype=torch.float32)
                # Crop/resize each frame before retaining the batch.
                images, masks = self._crop_frame(
                    frame, mask.unsqueeze(0), crop, aspect, use_megapixels,
                    megapixels, use_multiple, multiple)
                output_images.append(images)
                output_masks.append(masks)

        result_images = torch.cat(output_images, dim=0)
        result_masks = torch.cat(output_masks, dim=0)
        return (result_images, result_masks,
                result_images.shape[2], result_images.shape[1])

    @staticmethod
    def _crop_frame(images, masks, crop, aspect, use_megapixels,
                    megapixels, use_multiple, multiple):
        box = _parse_crop(crop, images.shape[2], images.shape[1])
        ratio = _aspect_value(aspect)
        if ratio is not None:
            if box is None:
                box = _centered_box(images.shape[2], images.shape[1], ratio)
                if box == (0, 0, images.shape[2], images.shape[1]):
                    box = None          # already exactly that shape
            else:
                # The editor keeps crop and aspect in step, so a mismatch
                # means a hand-edited or wired-in crop: refuse by name
                # rather than silently reshaping a selection. Tolerance
                # covers the normalized->pixel rounding, nothing more.
                bw, bh = box[2] - box[0], box[3] - box[1]
                if abs(bw - ratio * bh) > 2.0 * (1.0 + ratio):
                    raise ValueError(
                        "Load Image & Crop: the stored crop is %dx%d, "
                        "which is not %s. Redraw the crop with the "
                        "aspect set, or switch aspect back to 'Free'."
                        % (bw, bh, aspect))
        if box is not None:
            x0, y0, x1, y1 = box
            images = images[:, y0:y1, x0:x1, :]
            masks = masks[:, y0:y1, x0:x1]

        height, width = images.shape[1], images.shape[2]
        new_width, new_height = width, height

        if use_megapixels:
            target_pixels = megapixels * 1024 * 1024
            current_pixels = width * height
            ratio_for_resize = _aspect_value(aspect) or (width / height)
            if use_multiple:
                pair = ASPECT_RATIOS.get(aspect)
                new_width, new_height = _target_dimensions(
                    megapixels, ratio_for_resize, multiple, pair)
            else:
                # No multiple: use the ideal selected/crop ratio and round
                # width/height independently to whole pixels, matching the UI.
                ideal_height = math.sqrt(target_pixels / ratio_for_resize)
                ideal_width = ideal_height * ratio_for_resize
                new_width = max(1, round(ideal_width))
                new_height = max(1, round(ideal_height))
        elif use_multiple:
            # Normally the frontend already stores a multiple-aligned crop.
            # This fallback also handles old/hand-edited/wired crop JSON.
            if width % multiple or height % multiple:
                new_width = max(multiple, round(width / multiple) * multiple)
                new_height = max(multiple, round(height / multiple) * multiple)

        if new_width != width or new_height != height:
            import comfy.utils
            images = comfy.utils.common_upscale(
                images.movedim(-1, 1), new_width, new_height, "lanczos", "disabled"
            ).movedim(1, -1)
            masks = comfy.utils.common_upscale(
                masks.unsqueeze(1), new_width, new_height, "bilinear", "disabled"
            ).squeeze(1)

        return (images, masks)

    @classmethod
    def IS_CHANGED(cls, image, crop="", aspect="Free", use_megapixels=True,
                   megapixels=1.0, use_multiple=True, multiple=8):
        return hash_images([image])

    @classmethod
    def VALIDATE_INPUTS(cls, image, crop="", aspect="Free",
                        use_megapixels=True, megapixels=1.0,
                        use_multiple=True, multiple=8):
        try:
            image_path(image)
            finite_number(megapixels, "megapixels", 0.01, 128)
            int(finite_number(multiple, "multiple", 1, 1024))
            _aspect_value(aspect)
            _parse_crop(crop, 1, 1)
        except (ValueError, OSError) as exc:
            return str(exc)
        return True
