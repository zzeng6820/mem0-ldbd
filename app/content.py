"""AML content validation and the explicitly text-only image-skipping policy."""
from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_ADD_IMAGE_BYTES = 30 * 1024 * 1024
_DATA_URI = re.compile(r"^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]*={0,2})$")


def image_size(url: str) -> int:
    # The service never downloads or sends these images to another provider.
    match = _DATA_URI.fullmatch(url)
    if not match:
        raise ValueError("Images must be inline Base64 JPEG, PNG or WebP data URIs")
    encoded = match.group(2)
    # Bound allocations before decoding an untrusted string.
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise ValueError("Decoded image exceeds the 10 MiB limit")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Image contains invalid Base64") from None
    if not decoded:
        raise ValueError("Image must not be empty")
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError("Decoded image exceeds the 10 MiB limit")
    return len(decoded)


class TextPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Text part must not be blank")
        return value


class ImageURL(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str

    @field_validator("url")
    @classmethod
    def valid_image(cls, value):
        image_size(value)
        return value


class ImagePart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["image_url"]
    image_url: ImageURL


ContentPart = Annotated[TextPart | ImagePart, Field(discriminator="type")]
Content = Annotated[str, Field(min_length=1)] | Annotated[list[ContentPart], Field(min_length=1)]


def project_text(content: str | list[dict]) -> tuple[str, int]:
    """Retain every text part unchanged, in order; report ignored image count."""
    if isinstance(content, str):
        return content, 0
    text = []
    ignored = 0
    for part in content:
        if part["type"] == "text":
            text.append(part["text"])
        else:
            ignored += 1
    return "\n".join(text), ignored


def add_image_bytes(messages: list[dict]) -> int:
    return sum(image_size(part["image_url"]["url"])
               for message in messages if isinstance(message["content"], list)
               for part in message["content"] if part["type"] == "image_url")
