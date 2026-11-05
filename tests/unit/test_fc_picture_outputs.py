import base64
import cv2
import numpy as np
import pytest
from oxr.pipeline.models import Block, BlockType, ContentFormat
from oxr.pipeline.postprocess.picture_postprocess import postprocess_picture


def block():
    return Block(0, BlockType.PICTURE, [0,0,8,8], "", ContentFormat.TEXT)


def test_inline_image_is_a_real_decodable_png():
    image=np.full((8,8,3),37,dtype=np.uint8)
    result=postprocess_picture(block(),image,"x.png",0,"base64")
    assert result.content.startswith("![](data:image/png;base64,")
    payload=result.content.split("base64,",1)[1][:-1]
    decoded=cv2.imdecode(np.frombuffer(base64.b64decode(payload),dtype=np.uint8),cv2.IMREAD_COLOR)
    assert np.array_equal(decoded,image)
    assert "/tmp/" not in result.content


def test_none_does_not_publish_a_local_url():
    result=postprocess_picture(block(),np.zeros((8,8,3),dtype=np.uint8),"x",0,"none")
    assert result.content==""


def test_unknown_representation_fails():
    with pytest.raises(ValueError):postprocess_picture(block(),np.zeros((8,8,3),dtype=np.uint8),"x",0,"file")
