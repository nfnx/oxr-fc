import os
import base64
import cv2
import numpy as np
from oxr.pipeline.models import Block, ContentFormat
from oxr.config.settings import settings
from oxr.pipeline.image_assets import image_assets

def postprocess_picture(block: Block, image: np.ndarray, file_name: str, page_num: int,
                        output_format: str = "url") -> Block:
    """Honor API image representation; retain demo asset URLs and legacy default."""
    if output_format not in {"url", "base64", "none"}:
        raise ValueError("Unsupported picture representation")
    if output_format == "none":
        block.content = ""
        block.format = ContentFormat.MARKDOWN
        return block
    if output_format == "base64":
        ok, encoded = cv2.imencode(".png", image)
        if not ok:
            raise ValueError("Unable to encode an output image")
        uri = "data:image/png;base64," + base64.b64encode(encoded).decode("ascii")
        block.content = f"![]({uri})"
        block.format = ContentFormat.MARKDOWN
        return block
    assets = image_assets.get()
    if assets is not None:
        block.content = f"![]({assets.save(image)})"
        block.format = ContentFormat.MARKDOWN
        return block
    # filename without extension
    base_name = os.path.splitext(os.path.basename(file_name))[0]
    out_dir = os.path.join(settings.pipeline.image_output_dir, base_name, "imgs")
    os.makedirs(out_dir, exist_ok=True)
    
    img_name = f"{page_num}_{block.idx}.png"
    img_path = os.path.join(out_dir, img_name)
    cv2.imwrite(img_path, image)
    
    # create markdown reference
    block.content = f"![]({img_path})"
    block.format = ContentFormat.MARKDOWN
    return block
