"""PaddleOCR 3 adapter for Docling's PDF OCR stage."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

import numpy as np
from docling_core.types.doc import BoundingBox, CoordOrigin
from docling_core.types.doc.page import BoundingRectangle, TextCell

from docling.datamodel.accelerator_options import AcceleratorOptions
from docling.datamodel.base_models import Page
from docling.datamodel.document import ConversionResult
from docling.datamodel.pipeline_options import OcrOptions
from docling.models.base_ocr_model import BaseOcrModel
from docling.models.factories import get_ocr_factory
from docling.utils.profiling import TimeRecorder


class PaddleOcrOptions(OcrOptions):
    kind: ClassVar[str] = "form_lab_paddleocr"


class PaddleOcrModel(BaseOcrModel):
    def __init__(
        self,
        *,
        enabled: bool,
        artifacts_path: Path | None,
        options: PaddleOcrOptions,
        accelerator_options: AcceleratorOptions,
    ):
        super().__init__(
            enabled=enabled,
            artifacts_path=artifacts_path,
            options=options,
            accelerator_options=accelerator_options,
        )
        self.scale = options.scale
        self.reader = None
        if enabled:
            from paddleocr import PaddleOCR

            # Match the existing app's PaddleOCR initialization exactly.
            self.reader = PaddleOCR(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                enable_mkldnn=False,
            )

    @classmethod
    def get_options_type(cls) -> type[PaddleOcrOptions]:
        return PaddleOcrOptions

    def __call__(
        self, conv_res: ConversionResult, page_batch: Iterable[Page]
    ) -> Iterable[Page]:
        if not self.enabled:
            yield from page_batch
            return

        assert self.reader is not None
        for page in page_batch:
            if page._backend is None or not page._backend.is_valid():
                yield page
                continue
            with TimeRecorder(conv_res, "ocr"):
                cells: list[TextCell] = []
                for ocr_rect in self.get_ocr_rects(page):
                    if ocr_rect.area() == 0:
                        continue
                    image = page._backend.get_page_image(scale=self.scale, cropbox=ocr_rect)
                    for item in self.reader.predict(np.asarray(image)):
                        data = item.json
                        data = data.get("res", data)
                        for text, box, score in zip(
                            data.get("rec_texts", []),
                            data.get("rec_boxes", []),
                            data.get("rec_scores", []),
                        ):
                            x0, y0, x1, y1 = (float(value) for value in box)
                            cells.append(
                                TextCell(
                                    index=len(cells),
                                    text=text,
                                    orig=text,
                                    confidence=float(score),
                                    from_ocr=True,
                                    rect=BoundingRectangle.from_bounding_box(
                                        BoundingBox.from_tuple(
                                            coord=(
                                                x0 / self.scale + ocr_rect.l,
                                                y0 / self.scale + ocr_rect.t,
                                                x1 / self.scale + ocr_rect.l,
                                                y1 / self.scale + ocr_rect.t,
                                            ),
                                            origin=CoordOrigin.TOPLEFT,
                                        )
                                    ),
                                )
                            )
                self.post_process_cells(cells, page, conv_res)
            yield page


def register_paddleocr() -> None:
    """Register this local adapter with Docling's OCR factory."""
    factory = get_ocr_factory(allow_external_plugins=True)
    if PaddleOcrOptions not in factory.classes:
        factory.register(PaddleOcrModel, "form-lab-paddleocr", __name__)
