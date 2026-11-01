"""Image models for the Creative Producer: Muse Image scenes and SAM 3.1 segmentation.

Each call is logged as a model.call event and charged to the spend meter, like text calls.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from io import BytesIO
from time import perf_counter

from openai import AsyncOpenAI
from PIL import Image

from pgt.creative.compose import Box, parse_sam_output, png_bytes
from pgt.harness.policy import SpendCapExceeded
from pgt.llm import runtime
from pgt.llm.meta import IMAGE_MODEL, IMAGE_PRICE, SAM_MODEL, SAM_PRICE
from pgt.llm.runtime import Scope


class MediaClient:
    def __init__(
        self, client: AsyncOpenAI, image_model: str = IMAGE_MODEL, sam_model: str = SAM_MODEL
    ) -> None:
        self.client, self.image_model, self.sam_model = client, image_model, sam_model

    async def background(self, scope: Scope, role: str, prompt: str) -> Image.Image:
        """Generate one background scene. The prompt must not ask for text or the product."""
        return await self._call(
            scope, role, self.image_model, IMAGE_PRICE, {"prompt": prompt},
            lambda: self.client.images.generate(
                model=self.image_model, prompt=prompt, n=1, size="1024x1024",
                response_format="b64_json"),
            _image)

    async def scene(
        self, scope: Scope, role: str, prompt: str, reference: Image.Image
    ) -> Image.Image:
        """Render one 2:3 photograph with the product from `reference` inside it. The prompt
        calls the reference "@Image1"."""
        product = png_bytes(reference.convert("RGB"))
        return await self._call(
            scope, role, self.image_model, IMAGE_PRICE, {"prompt": prompt, "reference": True},
            lambda: self.client.images.edit(
                model=self.image_model, prompt=prompt, n=1,
                image=("product.png", product, "image/png"), size="1024x1536",
                response_format="b64_json"),
            _image)

    async def segment(self, scope: Scope, role: str, image: Image.Image, concept: str) -> list[Box]:
        """Find `concept` in `image` with SAM 3.1 and return its boxes."""
        data_url = "data:image/png;base64," + base64.b64encode(png_bytes(image)).decode()
        return await self._call(
            scope, role, self.sam_model, SAM_PRICE, {"concept": concept},
            lambda: self.client.responses.create(
                model=self.sam_model,
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": concept},
                    {"type": "input_image", "image_url": data_url},
                ]}]),
            lambda response: parse_sam_output(response.output_text))

    async def _call(self, scope: Scope, role: str, model: str, price: float, details: dict,
                    request: Callable[[], Awaitable], parse: Callable):
        """One image call under its deadline: a timeout is logged and retried once, a provider
        error or an unreadable answer is logged with its failure type and raised."""
        try:
            scope.meter.check(role, scope.loop_id)
        except SpendCapExceeded:
            self._log(scope, role, model, 0.0, perf_counter(), details, "spend_cap", 1)
            raise
        deadline = runtime.IMAGE_DEADLINE_S
        for attempt in (1, 2):
            started = perf_counter()
            try:
                response = await asyncio.wait_for(request(), deadline)
            except TimeoutError:
                self._log(scope, role, model, 0.0, started, details, "timeout", attempt, deadline)
                if attempt == 2:
                    raise runtime.ModelCallTimeout(
                        f"{model} ran past {deadline:g}s twice for {role}") from None
                continue
            except Exception:
                self._log(scope, role, model, 0.0, started, details, "provider", attempt, deadline)
                raise
            try:
                out = parse(response)
            except Exception:
                self._log(scope, role, model, price, started, details, "schema", attempt,
                          deadline)
                raise
            self._log(scope, role, model, price, started, details, None, attempt, deadline)
            return out
        raise AssertionError("unreachable")

    def _log(self, scope, role, model, cost, started, details, failure, attempt,
             deadline=None) -> None:
        scope.ws.events.append(
            "model.call",
            role,
            {
                "role": role,
                "model": model,
                "input_tokens": 0,
                "cached_tokens": 0,
                "output_tokens": 0,
                "searches": 0,
                "cost": cost,
                "wall_time_s": round(perf_counter() - started, 3),
                "attempt": attempt,
                "deadline_s": deadline,
                "ok": failure is None,
                "failure": failure,
                **details,
            },
            sim_day=scope.sim_day,
            loop_id=scope.loop_id,
        )
        if cost:
            scope.meter.record(role, scope.loop_id, cost, model=model)


def _image(result) -> Image.Image:
    image = Image.open(BytesIO(base64.b64decode(result.data[0].b64_json)))
    image.load()
    return image.convert("RGB")
