"""Image models for the Creative Producer: Muse Image scenes and SAM 3.1 segmentation.

Each call is logged as a model.call event and charged to the spend meter, like text calls.
"""

from __future__ import annotations

import base64
from io import BytesIO
from time import perf_counter

from openai import AsyncOpenAI
from PIL import Image

from pgt.creative.compose import Box, parse_sam_output, png_bytes
from pgt.llm.meta import IMAGE_MODEL, IMAGE_PRICE, SAM_MODEL, SAM_PRICE
from pgt.llm.runtime import Scope


class MediaClient:
    def __init__(
        self, client: AsyncOpenAI, image_model: str = IMAGE_MODEL, sam_model: str = SAM_MODEL
    ) -> None:
        self.client, self.image_model, self.sam_model = client, image_model, sam_model

    async def background(self, scope: Scope, role: str, prompt: str) -> Image.Image:
        """Generate one background scene. The prompt must not ask for text or the product."""
        scope.meter.check(role, scope.loop_id)
        started = perf_counter()
        ok = False
        try:
            result = await self.client.images.generate(
                model=self.image_model,
                prompt=prompt,
                n=1,
                size="1024x1024",
                response_format="b64_json",
            )
            image = Image.open(BytesIO(base64.b64decode(result.data[0].b64_json)))
            image.load()
            ok = True
            return image.convert("RGB")
        finally:
            self._log(
                scope,
                role,
                self.image_model,
                IMAGE_PRICE if ok else 0.0,
                started,
                ok,
                {"prompt": prompt},
            )

    async def scene(
        self, scope: Scope, role: str, prompt: str, reference: Image.Image
    ) -> Image.Image:
        """Render one 2:3 photograph with the product from `reference` inside it. The prompt
        calls the reference "@Image1"."""
        scope.meter.check(role, scope.loop_id)
        started = perf_counter()
        ok = False
        try:
            result = await self.client.images.edit(
                model=self.image_model,
                prompt=prompt,
                n=1,
                image=("product.png", png_bytes(reference.convert("RGB")), "image/png"),
                size="1024x1536",
                response_format="b64_json",
            )
            image = Image.open(BytesIO(base64.b64decode(result.data[0].b64_json)))
            image.load()
            ok = True
            return image.convert("RGB")
        finally:
            self._log(
                scope,
                role,
                self.image_model,
                IMAGE_PRICE if ok else 0.0,
                started,
                ok,
                {"prompt": prompt, "reference": True},
            )

    async def segment(self, scope: Scope, role: str, image: Image.Image, concept: str) -> list[Box]:
        """Find `concept` in `image` with SAM 3.1 and return its boxes."""
        scope.meter.check(role, scope.loop_id)
        started = perf_counter()
        ok = False
        data_url = "data:image/png;base64," + base64.b64encode(png_bytes(image)).decode()
        try:
            response = await self.client.responses.create(
                model=self.sam_model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": concept},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }
                ],
            )
            boxes = parse_sam_output(response.output_text)
            ok = True
            return boxes
        finally:
            self._log(
                scope,
                role,
                self.sam_model,
                SAM_PRICE if ok else 0.0,
                started,
                ok,
                {"concept": concept},
            )

    def _log(self, scope, role, model, cost, started, ok, details) -> None:
        scope.ws.events.append(
            "model.call",
            role,
            {
                "role": role,
                "model": model,
                "input_tokens": 0,
                "output_tokens": 0,
                "searches": 0,
                "cost": cost,
                "wall_time_s": round(perf_counter() - started, 3),
                "ok": ok,
                **details,
            },
            sim_day=scope.sim_day,
            loop_id=scope.loop_id,
        )
        if ok:
            scope.meter.record(role, scope.loop_id, cost, model=model)
